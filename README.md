# AI Shadow Government

Run in VS Code:

1. `python -m venv .venv`
2. Windows: `.venv\\Scripts\\activate`
3. `pip install -r requirements.txt`
4. `python app.py`
5. Open http://127.0.0.1:5000

Demo official: official@shadow.gov / Official@123

The app creates `shadow_government.db` and `uploads/` automatically. Set `SHADOW_SECRET_KEY` to a strong random value before real deployment and use HTTPS. The built-in AI is lightweight, deterministic decision support (keyword classification, priority, summarization, trend grouping and policy-risk heuristics), so the project runs without paid AI APIs.
