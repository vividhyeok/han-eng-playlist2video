# Playlist Lyric Video Pipeline

YouTube / YouTube Music 플레이리스트를 곡 단위로 검토한 뒤 한 번에 리릭비디오로 렌더하는 데스크톱 도구입니다.

## 현재 워크플로우

1. `https://music.youtube.com/playlist?list=...` 또는 `https://www.youtube.com/playlist?list=...` 형식의 플레이리스트 URL을 넣습니다.
2. 플레이리스트를 분석하면 고정된 내 채널 영상 목록을 먼저 자동 동기화한 뒤, 중복 업로드를 제외하고 트랙을 리스트업합니다.
3. 메인 화면에서는 상태와 요약만 보고, 각 곡의 세부 수정은 `곡 검토` 팝업에서 처리합니다.
4. 검토 팝업에서 제목, 아티스트, 앨범, 앨범아트 URL, YouTube URL, 가사를 수정할 수 있습니다.
5. 검토 팝업에서 자동 가사 찾기를 눌러 추가 소스에서 가사를 채우고, 그대로 사용할지 수정할지 고를 수 있습니다.
6. 가사를 붙여넣은 뒤 같은 팝업 안에서 수동 싱크 매핑 팝업을 열어 직접 LRC를 만들 수 있습니다.
7. 검토가 끝나면 선택된 트랙만 배치 렌더를 실행합니다.

기존 단건 검색, 수동 입력 다이얼로그, YouTube 후보 탐색 중심 UI는 메인 앱에서 제거했습니다.

## 가사 처리 방식

- 기본적으로 Genie를 먼저 확인하고, 없으면 LRCLIB, YouTube Music, SyncedLyrics 계열 소스를 순서대로 더 확인합니다.
- 싱크 가사를 찾으면 그대로 `.lrc`로 저장합니다.
- 싱크가 없더라도 일반 가사가 있으면 자막용 줄 정리를 거쳐 저장합니다.
- 트랙별 세부 작업은 검토 팝업으로 분리되어 메인 화면이 리스트 중심으로 유지됩니다.
- 자동으로 찾은 일반 가사는 그대로 저장하거나, 검토 후 수정해서 저장할 수 있습니다.
- 가사를 먼저 붙여넣고, 선택한 트랙의 YouTube 오디오를 기준으로 별도 싱크 매핑 팝업에서 직접 LRC를 만들 수 있습니다.
- 자동으로 가사를 못 찾은 곡은 목록에서 사라지지 않고 `가사 없음` 또는 `자동 해석 실패` 상태로 남습니다.
- 플레이리스트에서 가져온 YouTube URL을 각 트랙의 기본 오디오 소스로 유지합니다.
- 채널 중복 비교는 고정 채널 URL(`https://www.youtube.com/@korbittherabbit/videos`) 기준으로 자동 수행됩니다.

## 번역 처리 방식

- 번역은 OpenAI Responses API를 사용합니다.
- 선택한 번역 모델은 `data/config/config.json`에 저장됩니다.
- 곡별 번역 캐시는 `data/cache/translation_cache.json`에 저장됩니다.

## 요구 사항

- Python 3.11 이상 권장
- FFmpeg가 `PATH`에 있거나 프로젝트에서 인식 가능한 위치에 있어야 함
- OpenAI API 키

선택 사항:

- 플레이리스트 외 경로에서 파이프라인을 재사용할 경우 `spotdl`

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

프로젝트 루트에 `.env` 파일을 만들고 아래 값을 설정합니다.

```env
OPENAI_API_KEY=your_openai_api_key
```

## 실행

```bash
python main.py
```

## 출력 위치

- 임시 파일: `data/temp`
- 가사 파일: `data/lyrics`
- 렌더 결과물: `data/output`

## 참고

- 새 플레이리스트를 분석하면 현재 화면의 목록을 교체합니다.
- 배치 렌더 결과는 타임스탬프 기준 폴더로 묶입니다.
- YouTube 메타데이터 추출이 불안정하면 `yt-dlp`를 먼저 업데이트하세요.
