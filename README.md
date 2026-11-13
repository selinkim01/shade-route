# 그늘길 🌳

시각별 태양 위치와 건물·가로수 그림자를 계산해서, 체감온도가 25°C에 가장 가까운 도보 경로를 추천하는 데모입니다. 지도와 날씨는 더미 데이터입니다.

- 백엔드: `main.py` (FastAPI) — `/api/simulate`
- 프론트엔드: `static/index.html` (순수 HTML/JS, SVG 지도)

로컬 실행:

```
pip install -r requirements.txt
uvicorn main:app --reload
```

→ http://127.0.0.1:8000
