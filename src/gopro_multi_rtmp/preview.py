"""Session-scoped, dependency-free HLS preview dashboard."""

from __future__ import annotations

import html
import webbrowser
from pathlib import Path
from urllib.parse import quote

from .config import CameraConfig, PreviewConfig


def hls_url(preview: PreviewConfig, camera: CameraConfig) -> str:
    """Return the MediaMTX HLS player URL for one validated camera path."""
    stream_path = quote(f"live/{camera.stream_key}", safe="/")
    return (
        f"http://{preview.bind_host}:{preview.hls_port}/{stream_path}"
        "?muted=true&autoplay=true&controls=true&playsInline=true"
    )


def write_preview_dashboard(
    session_dir: Path,
    cameras: tuple[CameraConfig, ...],
    preview: PreviewConfig,
) -> Path:
    """Write a local responsive dashboard that embeds MediaMTX HLS players."""
    cards: list[str] = []
    for camera in cameras:
        alias = html.escape(camera.alias, quote=True)
        url = html.escape(hls_url(preview, camera), quote=True)
        cards.append(
            "\n".join(
                [
                    '<section class="camera">',
                    f"  <h2>{alias}</h2>",
                    (
                        f'  <iframe src="{url}" title="{alias} 实时画面" '
                        'allow="autoplay; fullscreen; picture-in-picture" '
                        'allowfullscreen></iframe>'
                    ),
                    "</section>",
                ]
            )
        )

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GoPro 多机实时预览</title>
  <style>
    :root {{ color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 18px; background: #101216; color: #f4f6f8; }}
    header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }}
    h1 {{ margin: 0 0 14px; font-size: 22px; }}
    header p {{ margin: 0; color: #aeb7c2; font-size: 13px; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }}
    .camera {{ overflow: hidden; border: 1px solid #303640; border-radius: 10px; background: #181c22; }}
    .camera h2 {{ margin: 0; padding: 9px 12px; font-size: 15px; font-weight: 600; }}
    iframe {{ display: block; width: 100%; aspect-ratio: 16 / 9; border: 0; background: #000; }}
  </style>
</head>
<body>
  <header>
    <h1>GoPro 多机实时预览</h1>
    <p>画面默认静音；录制停止后实时流会离线。</p>
  </header>
  <main>
    {''.join(cards)}
  </main>
</body>
</html>
"""
    target = session_dir / "runtime" / "preview.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def open_preview_dashboard(path: Path) -> bool:
    """Open the dashboard without making capture success depend on GUI support."""
    try:
        return bool(webbrowser.open_new_tab(path.resolve().as_uri()))
    except (OSError, webbrowser.Error):
        return False
