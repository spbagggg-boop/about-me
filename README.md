# ABOUT ME 상세페이지

공개 주소: https://spbagggg-boop.github.io/about-me/

## 구조

| 경로 | 설명 |
|---|---|
| `index.html` | 실제 서빙되는 페이지 (55KB) |
| `fonts/` | PretendardKR 4종 + Playfair Display, 이 페이지에 쓰인 글자만 남긴 서브셋 |
| `img/` | WebP로 변환한 이미지 |
| `video/` | 배경 영상 (오디오 제거 + 재인코딩) |
| `download.html` | 원본 단일파일 export (24.8MB). 오프라인 배포용 |
| `tools/unbundle.py` | export를 위 구조로 변환하는 스크립트 |

## 다시 만들 때

```bash
pip install "fonttools[woff]" pillow
python tools/unbundle.py "ABOUT-ME-mobile-standalone.html" . \
  --ffmpeg "C:/Users/82103/WhisperXXL/Faster-Whisper-XXL/ffmpeg.exe"
```

| 항목 | 전 | 후 |
|---|---|---|
| 폰트 | 10.85MB (TTF 4종 + woff2) | 257KB (서브셋 + woff2 변환) |
| 이미지 | 7.43MB PNG | 561KB WebP |
| 영상 | 6.19MB (4Mbps, 안 쓰는 오디오 포함) | 1.81MB (오디오 제거, CRF 24) |
| **전체** | **24.8MB** | **2.68MB** |

같은 스크립트로 LOVESICK 페이지도 변환한다. 그쪽은 720px 아트보드라
뷰포트 보정과 CTA 링크(`--cta`)가 추가로 붙는다.
