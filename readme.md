# Lyric Video Maker

YouTube / YouTube Music 플레이리스트를 분석해 한글 원문과 자연스러운 영문 번역이 포함된 가사 영상을 일괄 제작하는 Windows 데스크톱 앱입니다.

## 빠른 시작

1. [Releases](https://github.com/vividhyeok/han-eng-playlist2video/releases)에서 `LyricVideoMaker-Windows.exe`를 받습니다.
2. 앱을 실행하고 OpenAI API 키를 입력해 저장합니다.
3. 플레이리스트 URL을 붙여넣고 **플레이리스트 분석**을 누릅니다.
4. 대기열과 제외된 곡을 확인한 뒤 **전체 렌더링 시작**을 누릅니다.
5. **결과 폴더 열기**에서 완성된 MP4 또는 Premiere XML을 확인합니다.

별도 Python 설치는 필요하지 않습니다. FFmpeg와 FFprobe도 실행 파일에 포함됩니다.

## 번역과 렌더링

- 기본값은 품질과 처리량이 균형 잡힌 GPT-5.6 Terra입니다.
- 동일한 훅은 한 번만 번역해 표현을 통일하고 API 비용을 줄입니다.
- 긴 가사는 안정적인 크기로 나누고, 실패 시 지수 백오프로 재시도합니다.
- 문맥상 애매하거나 형식 검증에 실패한 구절만 GPT-5.6 Sol로 재검토합니다.
- 기존 영어, 고유명사, 브랜드, 애드리브와 라임 앵커를 보존합니다.
- 빠른 ASS 자막 렌더를 유지하면서 긴 줄과 동일 타임코드를 정리해 겹침을 방지합니다.
- 하드웨어 H.264 인코더를 우선 사용하고 필요할 때 `libx264`로 폴백합니다.

가사는 Genie를 우선 사용하고 LRCLIB로 보완합니다. 일반 가사는 Whisper 기반 자동 싱크를 시도하며, 오디오·앨범 아트·번역 결과를 캐시해 재실행 시간을 줄입니다.

## 소스에서 실행

Python 3.11 이상이 권장됩니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

설정과 캐시는 실행 파일 옆의 `data` 폴더에 저장됩니다. API 키는 실행 파일 옆 `.env`에 저장되며 Git에는 포함되지 않습니다.

## 자동 릴리스

`v*` 형식의 태그를 푸시하면 GitHub Actions가 Windows 단일 실행 파일을 빌드하고 시작 검사를 수행한 뒤 GitHub Releases에 자동 첨부합니다.
