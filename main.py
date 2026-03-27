import json
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

# .env 파일에서 FIGMA_TOKEN 등 환경변수 로드
load_dotenv()


class FigmaNodeFetcher:
    def __init__(self, access_token: str, timeout: int = 30):
        self.access_token = access_token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": access_token})

    @staticmethod
    def parse_figma_url(figma_url: str) -> tuple[str, str]:
        """
        Figma URL에서 file_key와 node_id를 추출한다.
        예:
        https://www.figma.com/design/Ec5VQIEWlZJe83dYtOhoND/Reppley-App-Design?node-id=22275-290006&t=...
        -> ("Ec5VQIEWlZJe83dYtOhoND", "22275:290006")
        """
        parsed = urlparse(figma_url)

        # /design/<file_key>/<file_name> 또는 /file/<file_key>/<file_name>
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            raise ValueError("Figma URL에서 file key를 찾을 수 없습니다.")

        # path_parts 예: ["design", "<FILE_KEY>", "<FILE_NAME>"]
        file_key = path_parts[1]

        qs = parse_qs(parsed.query)
        raw_node_id = qs.get("node-id", [None])[0]
        if not raw_node_id:
            raise ValueError("URL에 node-id 파라미터가 없습니다.")

        # API용 node id는 콜론 사용
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
            raise ValueError(
                f"노드 {node_id} 의 이미지 URL을 받지 못했습니다. 응답: {data}"
            )

        return image_url

    def download_file(self, url: str, output_path: str) -> str:
        resp = requests.get(url, timeout=self.timeout)
        self._raise_for_status(resp)

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(resp.content)
        return str(output.resolve())

    @staticmethod
    def extract_visible_texts(node: dict) -> list[str]:
        """
        노드 트리에서 TEXT.characters를 수집
        """
        texts = []

        def walk(n: dict):
            if n.get("type") == "TEXT":
                text = (n.get("characters") or "").strip()
                if text:
                    texts.append(text)
            for child in n.get("children", []) or []:
                walk(child)

        walk(node)

        # 중복 제거, 순서 유지
        seen = set()
        deduped = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped

    @staticmethod
    def extract_interactive_candidates(node: dict) -> list[dict]:
        """
        아주 단순한 규칙으로 인터랙션 후보 추출
        - INSTANCE 이름에 Button 포함
        - TEXT가 '더보기', '확인', '다음', '저장' 같은 CTA 후보
        """
        candidates = []
        cta_keywords = {
            "더보기",
            "확인",
            "다음",
            "저장",
            "완료",
            "닫기",
            "취소",
            "공유",
            "삭제",
            "수정",
        }

        def walk(n: dict):
            node_type = n.get("type", "")
            name = (n.get("name") or "").strip()

            if node_type == "INSTANCE" and "button" in name.lower():
                candidates.append(
                    {"type": "button_candidate", "name": name, "id": n.get("id")}
                )

            if node_type == "TEXT":
                text = (n.get("characters") or "").strip()
                if text in cta_keywords:
                    candidates.append(
                        {"type": "cta_text_candidate", "label": text, "id": n.get("id")}
                    )

            for child in n.get("children", []) or []:
                walk(child)

        walk(node)
        return candidates

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


def main():
    FIGMA_TOKEN = os.environ.get("FIGMA_TOKEN", "").strip()
    FIGMA_URL = "https://www.figma.com/design/Ec5VQIEWlZJe83dYtOhoND/Reppley-App-Design?node-id=22275-290002&t=o2wDleDIIn5ERGQA-0"

    if not FIGMA_TOKEN:
        raise ValueError(".env 또는 환경변수 FIGMA_TOKEN 에 액세스 토큰을 넣어주세요.")

    fetcher = FigmaNodeFetcher(FIGMA_TOKEN)

    file_key, node_id = fetcher.parse_figma_url(FIGMA_URL)
    print("file_key =", file_key)
    print("node_id  =", node_id)

    # 1) 노드 JSON 가져오기
    node_json = fetcher.get_node_json(file_key, node_id)

    # 저장
    Path("out").mkdir(exist_ok=True)
    with open("out/figma_node.json", "w", encoding="utf-8") as f:
        json.dump(node_json, f, ensure_ascii=False, indent=2)

    # 2) 노드 이미지 URL 가져오기
    image_url = fetcher.get_node_image_url(
        file_key, node_id, image_format="png", scale=2
    )
    print("image_url =", image_url)

    # 3) 이미지 다운로드
    image_path = fetcher.download_file(image_url, "out/figma_node.png")
    print("saved image =", image_path)

    # 4) 화면 텍스트와 기능 후보 간단 추출
    visible_texts = fetcher.extract_visible_texts(node_json)
    interactive_candidates = fetcher.extract_interactive_candidates(node_json)

    summary = {
        "file_key": file_key,
        "node_id": node_id,
        "image_path": image_path,
        "visible_texts": visible_texts,
        "interactive_candidates": interactive_candidates,
    }

    with open("out/summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== visible_texts ===")
    for t in visible_texts[:50]:
        print("-", t)

    print("\n=== interactive_candidates ===")
    for c in interactive_candidates:
        print("-", c)


if __name__ == "__main__":
    main()
