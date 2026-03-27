import os
from urllib.parse import urlparse, parse_qs
import base64

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI(title="Figma Bridge WebApp", version="0.1.0")

# Set up templates and static files
templates = Jinja2Templates(directory="templates")

class FigmaRequest(BaseModel):
    figma_url: str

class FigmaNodeFetcher:
    def __init__(self, access_token: str, timeout: int = 30):
        self.access_token = access_token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": access_token})

    @staticmethod
    def parse_figma_url(figma_url: str) -> tuple[str, str]:
        """Extract file_key and node_id from Figma URL"""
        parsed = urlparse(figma_url)

        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError("Figma URL에서 file key를 찾을 수 없습니다.")

        file_key = path_parts[1]

        qs = parse_qs(parsed.query)
        raw_node_id = qs.get("node-id", [None])[0]
        if not raw_node_id:
            raise ValueError("URL에 node-id 파라미터가 없습니다.")

        node_id = raw_node_id.replace("-", ":")
        return file_key, node_id

    def get_node_json(self, file_key: str, node_id: str) -> dict:
        url = f"https://api.figma.com/v1/files/{file_key}/nodes"
        resp = self.session.get(
            url,
            params={"ids": node_id},
            timeout=self.timeout,
        )
        self._raise_for_status(resp)

        data = resp.json()
        if "nodes" not in data or node_id not in data["nodes"]:
            raise ValueError(f"응답에서 노드 {node_id} 를 찾지 못했습니다.")

        return data["nodes"][node_id]["document"]

    def get_node_image_url(
        self, file_key: str, node_id: str, image_format: str = "png", scale: int = 2
    ) -> str:
        url = f"https://api.figma.com/v1/images/{file_key}"
        resp = self.session.get(
            url,
            params={
                "ids": node_id,
                "format": image_format,
                "scale": scale,
            },
            timeout=self.timeout,
        )
        self._raise_for_status(resp)

        data = resp.json()
        image_url = data.get("images", {}).get(node_id)
        if not image_url:
            raise ValueError(f"노드 {node_id} 의 이미지 URL을 받지 못했습니다.")

        return image_url

    def download_image_as_base64(self, image_url: str) -> str:
        """Download image and return as base64 string"""
        resp = requests.get(image_url, timeout=self.timeout)
        self._raise_for_status(resp)

        image_base64 = base64.b64encode(resp.content).decode('utf-8')
        return f"data:image/png;base64,{image_base64}"

    @staticmethod
    def extract_visible_texts(node: dict) -> list[str]:
        """Extract visible text from node tree"""
        texts = []

        def walk(n: dict):
            if n.get("type") == "TEXT":
                text = (n.get("characters") or "").strip()
                if text:
                    texts.append(text)
            for child in n.get("children", []) or []:
                walk(child)

        walk(node)

        # Remove duplicates while preserving order
        seen = set()
        deduped = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped

    @staticmethod
    def extract_components_and_layout(node: dict) -> dict:
        """Extract component and layout information with styles"""
        components = []
        layout_info = {
            "screen_name": node.get("name", ""),
            "type": node.get("type", ""),
            "width": node.get("absoluteBoundingBox", {}).get("width", 0),
            "height": node.get("absoluteBoundingBox", {}).get("height", 0),
        }

        def extract_fills(fills):
            """Extract fill information (colors, gradients, etc.)"""
            if not fills:
                return None

            fill_info = []
            for fill in fills:
                if fill.get("type") == "SOLID":
                    color = fill.get("color", {})
                    opacity = fill.get("opacity", 1)
                    fill_info.append({
                        "type": "solid",
                        "color": {
                            "r": int(color.get("r", 0) * 255),
                            "g": int(color.get("g", 0) * 255),
                            "b": int(color.get("b", 0) * 255),
                            "a": color.get("a", 1) * opacity
                        },
                        "hex": f"#{int(color.get('r', 0)*255):02x}{int(color.get('g', 0)*255):02x}{int(color.get('b', 0)*255):02x}"
                    })
                elif fill.get("type") == "GRADIENT_LINEAR":
                    fill_info.append({
                        "type": "gradient",
                        "gradient_type": "linear",
                        "stops": fill.get("gradientStops", [])
                    })
            return fill_info

        def extract_text_style(style):
            """Extract text styling information"""
            if not style:
                return None

            return {
                "font_family": style.get("fontFamily"),
                "font_size": style.get("fontSize"),
                "font_weight": style.get("fontWeight"),
                "line_height": style.get("lineHeightPx"),
                "letter_spacing": style.get("letterSpacing"),
                "text_align": style.get("textAlignHorizontal"),
                "text_decoration": style.get("textDecoration")
            }

        def extract_effects(effects):
            """Extract effects like shadows, blurs"""
            if not effects:
                return None

            effect_info = []
            for effect in effects:
                if effect.get("type") == "DROP_SHADOW":
                    color = effect.get("color", {})
                    effect_info.append({
                        "type": "drop_shadow",
                        "offset": effect.get("offset", {}),
                        "radius": effect.get("radius", 0),
                        "color": {
                            "r": int(color.get("r", 0) * 255),
                            "g": int(color.get("g", 0) * 255),
                            "b": int(color.get("b", 0) * 255),
                            "a": color.get("a", 1)
                        }
                    })
                elif effect.get("type") == "INNER_SHADOW":
                    color = effect.get("color", {})
                    effect_info.append({
                        "type": "inner_shadow",
                        "offset": effect.get("offset", {}),
                        "radius": effect.get("radius", 0),
                        "color": {
                            "r": int(color.get("r", 0) * 255),
                            "g": int(color.get("g", 0) * 255),
                            "b": int(color.get("b", 0) * 255),
                            "a": color.get("a", 1)
                        }
                    })
            return effect_info if effect_info else None

        def walk(n: dict, depth: int = 0):
            node_type = n.get("type", "")
            name = n.get("name", "")
            bounds = n.get("absoluteBoundingBox", {})

            # Extract basic component info
            component_info = {
                "name": name,
                "type": node_type,
                "depth": depth,
                "x": bounds.get("x", 0),
                "y": bounds.get("y", 0),
                "width": bounds.get("width", 0),
                "height": bounds.get("height", 0),
            }

            # Add styling information based on node type
            if node_type == "TEXT":
                component_info.update({
                    "text_content": n.get("characters", ""),
                    "text_style": extract_text_style(n.get("style")),
                    "fills": extract_fills(n.get("fills"))
                })

            elif node_type in ["FRAME", "COMPONENT", "INSTANCE", "GROUP"]:
                component_info.update({
                    "fills": extract_fills(n.get("fills")),
                    "strokes": extract_fills(n.get("strokes")),
                    "corner_radius": n.get("cornerRadius"),
                    "effects": extract_effects(n.get("effects")),
                    "opacity": n.get("opacity", 1),
                    "blend_mode": n.get("blendMode"),
                    "layout_mode": n.get("layoutMode"),
                    "padding": {
                        "top": n.get("paddingTop", 0),
                        "right": n.get("paddingRight", 0),
                        "bottom": n.get("paddingBottom", 0),
                        "left": n.get("paddingLeft", 0)
                    } if n.get("paddingTop") is not None else None
                })

            elif node_type == "RECTANGLE":
                component_info.update({
                    "fills": extract_fills(n.get("fills")),
                    "strokes": extract_fills(n.get("strokes")),
                    "corner_radius": n.get("cornerRadius"),
                    "effects": extract_effects(n.get("effects")),
                    "opacity": n.get("opacity", 1)
                })

            # Only add components that have meaningful information
            if node_type in ["COMPONENT", "INSTANCE", "FRAME", "GROUP", "TEXT", "RECTANGLE"]:
                components.append(component_info)

            for child in n.get("children", []) or []:
                walk(child, depth + 1)

        walk(node)

        return {
            "layout": layout_info,
            "components": components
        }

    @staticmethod
    def generate_vibe_coding_text(screen_name: str, texts: list[str], components: list[dict]) -> str:
        """Generate enhanced vibe coding formatted text with styling info"""
        vibe_text = f"# {screen_name} 화면 구현\n\n"

        # Add screen dimensions
        screen_component = next((c for c in components if c["depth"] == 0), None)
        if screen_component:
            vibe_text += f"**화면 크기:** {screen_component['width']}px × {screen_component['height']}px\n\n"

        # Text content section
        vibe_text += "## 📝 화면 텍스트 내용\n"
        for text in texts[:15]:
            vibe_text += f"- \"{text}\"\n"

        # Color palette section
        vibe_text += "\n## 🎨 주요 색상 정보\n"
        colors_found = set()
        for comp in components:
            if comp.get("fills"):
                for fill in comp["fills"]:
                    if fill.get("type") == "solid" and fill.get("hex"):
                        colors_found.add(fill["hex"])

        for color in sorted(colors_found)[:8]:  # Top 8 colors
            vibe_text += f"- {color}\n"

        # Typography section
        vibe_text += "\n## 📱 텍스트 스타일\n"
        text_components = [c for c in components if c["type"] == "TEXT" and c.get("text_style")]
        unique_text_styles = {}

        for comp in text_components[:10]:
            text_style = comp.get("text_style", {})
            if text_style.get("font_size"):
                style_key = f"{text_style.get('font_size')}px"
                if text_style.get("font_weight"):
                    style_key += f" {text_style.get('font_weight')}"

                if style_key not in unique_text_styles:
                    unique_text_styles[style_key] = {
                        "size": text_style.get("font_size"),
                        "weight": text_style.get("font_weight"),
                        "family": text_style.get("font_family"),
                        "example": comp.get("text_content", "")[:20]
                    }

        for style_name, style_info in unique_text_styles.items():
            example = f" (예: \"{style_info['example']}\")" if style_info['example'] else ""
            vibe_text += f"- {style_name} {style_info.get('family', '')}{example}\n"

        # Layout structure
        vibe_text += "\n## 🏗️ 레이아웃 구조\n"
        main_components = [c for c in components if c["type"] in ["FRAME", "COMPONENT", "INSTANCE"] and c["depth"] <= 2]
        for comp in main_components[:8]:
            indent = "  " * comp["depth"]
            size_info = f" ({comp['width']}×{comp['height']})" if comp['width'] > 0 else ""
            vibe_text += f"{indent}- {comp['name']} [{comp['type']}]{size_info}\n"

        # Interactive elements
        vibe_text += "\n## 📱 인터랙티브 요소\n"
        interactive_elements = [c for c in components if
                              "button" in c["name"].lower() or
                              c["type"] == "INSTANCE" or
                              ("text" in c.get("name", "").lower() and any(keyword in c.get("text_content", "").lower()
                                  for keyword in ["버튼", "클릭", "확인", "취소", "다음", "이전"]))]

        for element in interactive_elements[:5]:
            action_type = "Button" if "button" in element["name"].lower() else "Touchable"
            vibe_text += f"- {element['name']}: {element.get('text_content', '터치 가능 영역')} [{action_type}]\n"

        # Native-specific sections
        vibe_text += "\n## 📐 네이티브 레이아웃 가이드\n"
        if screen_component:
            # Convert px to dp/pt
            width_dp = round(screen_component['width'] / 3)  # Rough conversion for Android dp
            width_pt = round(screen_component['width'] / 3)  # Rough conversion for iOS pt
            vibe_text += f"- **Android:** {width_dp} dp 기준 레이아웃\n"
            vibe_text += f"- **iOS:** {width_pt} pt 기준 레이아웃\n"
            vibe_text += f"- Safe Area 고려 필요\n"
            vibe_text += f"- Status Bar 높이 대응\n\n"

        vibe_text += "## 🎯 네이티브 컴포넌트 매핑\n"
        component_mapping = {
            "FRAME": "Container/View",
            "TEXT": "TextView/Text",
            "INSTANCE": "Custom Component",
            "RECTANGLE": "View/Shape",
            "GROUP": "ViewGroup/Group"
        }

        mapped_components = set()
        for comp in components[:10]:
            comp_type = comp["type"]
            if comp_type in component_mapping and comp_type not in mapped_components:
                vibe_text += f"- **{comp_type}** → iOS: {component_mapping[comp_type].split('/')[1] if '/' in component_mapping[comp_type] else component_mapping[comp_type]}, Android: {component_mapping[comp_type].split('/')[0] if '/' in component_mapping[comp_type] else component_mapping[comp_type]}\n"
                mapped_components.add(comp_type)

        # Implementation guide
        vibe_text += "\n## 🚀 네이티브 앱 구현 가이드\n"
        vibe_text += "이 화면을 네이티브 앱(iOS/Android)으로 구현해주세요.\n\n"
        vibe_text += "**iOS (Swift/SwiftUI):**\n"
        vibe_text += "- VStack, HStack, ZStack을 활용한 레이아웃\n"
        vibe_text += "- Color 익스텐션으로 색상 팔레트 정의\n"
        vibe_text += "- Font.custom()으로 텍스트 스타일 적용\n"
        vibe_text += "- @State, @Binding으로 상태 관리\n\n"
        vibe_text += "**Android (Kotlin/Jetpack Compose):**\n"
        vibe_text += "- Column, Row, Box를 활용한 레이아웃\n"
        vibe_text += "- Color 리소스로 색상 팔레트 정의\n"
        vibe_text += "- TextStyle로 타이포그래피 설정\n"
        vibe_text += "- remember, mutableStateOf로 상태 관리\n\n"
        vibe_text += "**공통 사항:**\n"
        vibe_text += "- 네이티브 네비게이션 패턴 적용\n"
        vibe_text += "- 플랫폼별 디자인 가이드라인 준수 (HIG/Material Design)\n"
        vibe_text += "- 터치 제스처 및 햅틱 피드백\n"
        vibe_text += "- 다크모드 지원\n"
        vibe_text += "- 접근성(Accessibility) 지원\n"

        return vibe_text

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            detail = ""
            try:
                detail = f"\nResponse body: {resp.text}"
            except Exception:
                pass
            raise requests.HTTPError(f"{e}{detail}") from e

@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    """Main page with Figma URL input"""
    return templates.TemplateResponse(request, "index.html")

@app.post("/api/extract")
async def extract_figma_data(request: FigmaRequest):
    """Extract data from Figma URL"""
    try:
        # Get Figma token from environment
        figma_token = os.environ.get("FIGMA_TOKEN")
        if not figma_token:
            raise HTTPException(status_code=500, detail="FIGMA_TOKEN이 설정되지 않았습니다.")

        # Initialize fetcher
        fetcher = FigmaNodeFetcher(figma_token)

        # Parse URL
        file_key, node_id = fetcher.parse_figma_url(request.figma_url)

        # Get node data
        node_json = fetcher.get_node_json(file_key, node_id)

        # Get image
        image_url = fetcher.get_node_image_url(file_key, node_id)
        image_base64 = fetcher.download_image_as_base64(image_url)

        # Extract information
        texts = fetcher.extract_visible_texts(node_json)
        component_info = fetcher.extract_components_and_layout(node_json)

        # Generate structured JSON schema
        json_schema = {
            "screen_name": component_info["layout"]["screen_name"],
            "dimensions": {
                "width": component_info["layout"]["width"],
                "height": component_info["layout"]["height"]
            },
            "texts": texts,
            "components": component_info["components"],
            "figma_info": {
                "file_key": file_key,
                "node_id": node_id,
                "url": request.figma_url
            }
        }

        # Generate vibe coding text
        vibe_text = fetcher.generate_vibe_coding_text(
            component_info["layout"]["screen_name"],
            texts,
            component_info["components"]
        )

        return JSONResponse({
            "success": True,
            "data": {
                "image": image_base64,
                "json_schema": json_schema,
                "vibe_coding_text": vibe_text
            }
        })

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"처리 중 오류가 발생했습니다: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8100, reload=True)