# Clarifier State-Point & Solids Flux Analysis

Streamlit app for operators to run dynamic state-point / solids flux analysis
on secondary clarifiers, including what-if scenarios for taking units online/offline.

## Deploy to Streamlit Community Cloud (free, public URL, no infra)

1. Create a new GitHub repo (public or private, both work) and push everything in this
   folder, keeping the folder structure intact:
   - `app.py`
   - `requirements.txt`
   - `assets/inframark_logo.webp` (light-mode logo, navy text)
   - `assets/inframark_logo_dark.webp` (dark-mode logo, white text, generated to match)
   - `.streamlit/config.toml` (brand accent color only, doesn't force light or dark, people can pick either)
   - `README.md` (optional)
2. Go to https://share.streamlit.io, sign in with GitHub.
3. Click "New app," pick the repo/branch, set main file path to `app.py`.
4. Click Deploy. You'll get a URL like `https://your-app-name.streamlit.app` you can
   drop straight into an email, Teams channel, or intranet page.
5. Any time you push a change to the repo, the app redeploys automatically.

Note: some editors and file explorers hide folders starting with a dot (like `.streamlit`)
by default. Make sure it actually gets committed and pushed, GitHub's web upload UI in
particular can silently skip it if you drag-and-drop instead of using git directly.

## Run it locally first (optional, to sanity check before deploying)

```
pip install -r requirements.txt
streamlit run app.py
```

## Notes

- No login/auth built in. If that matters, Streamlit Community Cloud supports
  restricting access to specific emails under app settings, or ask IT about
  hosting internally instead if it needs to sit behind the company network.
- All calculation happens client-session-side, nothing is saved between sessions.
  If you want operators to save/reload site configs later, that's a v2 feature
  (needs a small database or file-based storage backend).
