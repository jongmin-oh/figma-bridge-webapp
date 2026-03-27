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
        """Extract component and layout information"""
        components = []
        layout_info = {
            "screen_name": node.get("name", ""),
            "type": node.get("type", ""),
            "width": node.get("absoluteBoundingBox", {}).get("width", 0),
            "height": node.get("absoluteBoundingBox", {}).get("height", 0),
        }

        def walk(n: dict, depth: int = 0):
            node_type = n.get("type", "")
            name = n.get("name", "")

            if node_type in ["COMPONENT", "INSTANCE", "FRAME", "GROUP"]:
                bounds = n.get("absoluteBoundingBox", {})
                components.append({
                    "name": name,
                    "type": node_type,
                    "depth": depth,
                    "x": bounds.get("x", 0),
                    "y": bounds.get("y", 0),
                    "width": bounds.get("width", 0),
                    "height": bounds.get("height", 0),
                })

            for child in n.get("children", []) or []:
                walk(child, depth + 1)

        walk(node)

        return {
            "layout": layout_info,
            "components": components
        }

    @staticmethod
    def generate_vibe_coding_text(screen_name: str, texts: list[str], components: list[dict]) -> str:
        """Generate vibe coding formatted text"""
        vibe_text = f"# {screen_name} 화면 구현\n\n"

        vibe_text += "## 화면 구성 요소\n"
        for text in texts[:20]:  # Limit to first 20 texts
            vibe_text += f"- {text}\n"

        vibe_text += "\n## 주요 컴포넌트\n"
        main_components = [c for c in components if c["type"] in ["COMPONENT", "INSTANCE", "FRAME"]]
        for comp in main_components[:10]:  # Limit to first 10 components
            vibe_text += f"- {comp['name']} ({comp['type']})\n"

        vibe_text += "\n## 구현 가이드\n"
        vibe_text += "이 화면을 React 컴포넌트로 구현해주세요.\n"
        vibe_text += "- 반응형 디자인을 고려하여 구현\n"
        vibe_text += "- 적절한 상태 관리 적용\n"
        vibe_text += "- 접근성을 고려한 마크업 사용\n"

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
    return templates.TemplateResponse("index.html", {"request": request})

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