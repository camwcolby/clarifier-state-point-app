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

## Enable "save this facility's clarifier setup" (one-time setup)

The Facility section at the top of the app lets operators save/load clarifier dimensions
by site name, so nobody re-types tank sizes every day. This writes small JSON files back
into this same GitHub repo, so it needs a token to do that on your behalf.

1. On GitHub, go to Settings (your profile menu, not the repo) > Developer settings >
   Personal access tokens > Fine-grained tokens > Generate new token.
2. Give it a name like "clarifier app save/load," set an expiration (a year is fine, GitHub
   will remind you before it lapses), and under Repository access, select only this repo.
3. Under Permissions, find "Contents" and set it to Read and write. That's the only
   permission it needs.
4. Generate the token and copy it, GitHub only shows it once.
5. In Streamlit Community Cloud, open this app's settings (the "..." menu on your app,
   or from the dashboard), go to Secrets, and paste in:

```
[github]
token = "paste_your_token_here"
repo = "your-github-username/clarifier-state-point-app"
branch = "main"
```

6. Save. The app will restart automatically and the Facility section will switch from
   "not set up yet" to showing live Save/Load controls.

Saved configs land in a `saved_facilities/` folder in the repo, one JSON file per site,
auto-created the first time anyone saves. Since it's a normal git folder, you get full
version history on it for free, check the repo's commit log if a facility's numbers
ever look off and you want to see what changed.

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
