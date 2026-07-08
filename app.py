import streamlit as st
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os
import re
import json
import base64
import requests
from datetime import datetime, timezone

st.set_page_config(page_title="Clarifier State-Point Analysis", layout="wide")

# Brand palette (from Cameron's Power BI theme file)
NAVY = "#0D004C"
BLUE = "#008FD5"
GREEN = "#5EB95E"
LIME = "#AFE327"
TEAL = "#75AABB"
GRAY = "#45484D"
FAIL_RED = "#C0392B"  # not in the brand file, kept separate on purpose --
                       # pass/fail needs to read as green/red at a glance, brand navy/blue don't carry that meaning

st.markdown(
    f"""
    <style>
    .req-note {{ font-size: 0.85rem; opacity: 0.75; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------- core math -----------------------------------
# (unchanged from the tested version)

def vesilind_vs(X_gL, Vo, k):
    """Zone settling velocity, ft/hr. X in g/L."""
    return Vo * np.exp(-k * X_gL)

def gravity_flux(X_mgL, Vo, k):
    """Gravity (batch) solids flux, lb/day/ft2. X in mg/L."""
    X_gL = X_mgL / 1000.0
    Vs = vesilind_vs(X_gL, Vo, k)
    gpd_per_ft2 = Vs * 7.4805 * 24.0
    return X_mgL * gpd_per_ft2 * 8.34 / 1e6

def applied_flux(Q_plus_QR_mgd, Xa_mgL, area_ft2):
    if area_ft2 <= 0:
        return np.inf
    return (Q_plus_QR_mgd * Xa_mgL * 8.34) / area_ft2

def underflow_conc(Xa_mgL, Q_mgd, QR_mgd, measured_Xu=None):
    if measured_Xu:
        return measured_Xu
    if QR_mgd <= 0:
        return np.nan
    return Xa_mgL * (Q_mgd + QR_mgd) / QR_mgd

def check_capacity(Xa_mgL, Xu_mgL, QR_mgd, area_ft2, Vo, k, applied_flux_ref=None, n=400):
    """Direct check: does the required underflow removal line stay at/below
    the gravity flux curve for all X between Xa and Xu? margin_pct is normalized
    against the applied flux (a stable reference) rather than the local line value,
    which can approach zero near Xu and blow up the percentage otherwise."""
    if area_ft2 <= 0 or Xu_mgL <= Xa_mgL:
        return False, -100.0, None, None, None
    QR_gpd_per_ft2 = QR_mgd * 1e6 / area_ft2
    Xs = np.linspace(Xa_mgL, Xu_mgL, n)
    gravity = gravity_flux(Xs, Vo, k)
    line = (Xu_mgL - Xs) * QR_gpd_per_ft2 * 8.34 / 1e6
    gap = gravity - line
    ok = bool(np.min(gap) >= 0)
    worst_idx = int(np.argmin(gap))
    ref = applied_flux_ref if applied_flux_ref else max(gravity[0], 1e-6)
    margin_pct = float((gap[worst_idx] / ref) * 100.0)
    return ok, margin_pct, Xs, gravity, line

def min_required_area(Xa_mgL, Xu_mgL, QR_mgd, Vo, k, area_lo=50, area_hi=300000, tol=1.0):
    ok_hi, *_ = check_capacity(Xa_mgL, Xu_mgL, QR_mgd, area_hi, Vo, k)
    if not ok_hi:
        return None
    ok_lo, *_ = check_capacity(Xa_mgL, Xu_mgL, QR_mgd, area_lo, Vo, k)
    if ok_lo:
        return area_lo
    lo, hi = area_lo, area_hi
    while hi - lo > tol:
        mid = (lo + hi) / 2
        ok, *_ = check_capacity(Xa_mgL, Xu_mgL, QR_mgd, mid, Vo, k)
        if ok:
            hi = mid
        else:
            lo = mid
    return hi

def mlss_threshold_from_current(Q_mgd, QR_mgd, area_ft2, Vo, k, current_mlss, currently_ok,
                                 measured_Xu=None, ceiling=50000, floor=50):
    """
    Find how far MLSS could move from the CURRENT operating point before the pass/fail
    status flips. Always anchored at current_mlss (which must match currently_ok, the
    same boolean already computed for the main system check), so this can never report
    a threshold that contradicts the main result. Walks outward and only refines locally,
    no assumption that pass/fail is monotonic across the whole MLSS range.
    Returns (threshold_mlss, found: bool). found=False means no flip within the search range.
    """
    bound = ceiling if currently_ok else floor
    if (currently_ok and current_mlss >= bound) or (not currently_ok and current_mlss <= bound):
        return bound, False

    coarse = np.linspace(current_mlss, bound, 200)
    flip_at = None
    for i, m in enumerate(coarse):
        if m <= 0:
            continue
        xu = underflow_conc(m, Q_mgd, QR_mgd, measured_Xu)
        ok, *_ = check_capacity(m, xu, QR_mgd, area_ft2, Vo, k)
        if ok != currently_ok:
            flip_at = i
            break
    if flip_at is None:
        return bound, False

    lo, hi = coarse[max(flip_at - 1, 0)], coarse[flip_at]
    for _ in range(40):
        mid = (lo + hi) / 2
        xu = underflow_conc(mid, Q_mgd, QR_mgd, measured_Xu)
        ok, *_ = check_capacity(mid, xu, QR_mgd, area_ft2, Vo, k)
        if ok == currently_ok:
            lo = mid
        else:
            hi = mid
    return lo, True

def clarifier_area(row):
    if row["Shape"] == "Circular":
        d = row.get("Diameter (ft)", 0) or 0
        return np.pi * (d / 2) ** 2
    else:
        l = row.get("Length (ft)", 0) or 0
        w = row.get("Width (ft)", 0) or 0
        return l * w

def default_clarifier_template():
    return pd.DataFrame(
        [
            {"Name": "Clarifier 1", "Shape": "Circular", "Diameter (ft)": 80.0,
             "Length (ft)": None, "Width (ft)": None, "Online?": True, "Flow Split Override (%)": None},
            {"Name": "Clarifier 2", "Shape": "Circular", "Diameter (ft)": 80.0,
             "Length (ft)": None, "Width (ft)": None, "Online?": True, "Flow Split Override (%)": None},
        ]
    )

def req(label):
    """Append a required-field marker to a widget label."""
    return f"{label}  :red[*]"

def status_banner(ok, margin, headline):
    """Big, hard-to-miss pass/fail banner for operators glancing at a phone."""
    bg = GREEN if ok else FAIL_RED
    icon = "\u2705" if ok else "\u26A0\ufe0f"
    title = "SYSTEM OK" if ok else "OVERLOADED"
    st.markdown(
        f"""
        <div style="background:{bg};padding:1.1rem 1.4rem;border-radius:10px;margin:0.5rem 0 1rem 0;">
          <div style="color:white;font-size:1.7rem;font-weight:700;line-height:1.2;">{icon} {title}</div>
          <div style="color:white;font-size:1.05rem;margin-top:0.35rem;">{headline}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ----------------------------- facility save/load (GitHub-backed) -----------

SAVE_DIR = "saved_facilities"

def _slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "unnamed"

def _gh_config():
    """Reads GitHub repo/token from Streamlit secrets. Returns (token, repo, branch)
    or (None, None, None) if not configured, so the app degrades gracefully."""
    try:
        token = st.secrets["github"]["token"]
        repo = st.secrets["github"]["repo"]  # format: "owner/repo-name"
        branch = st.secrets["github"].get("branch", "main")
        return token, repo, branch
    except Exception:
        return None, None, None

def _gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def save_load_configured():
    token, *_ = _gh_config()
    return token is not None

@st.cache_data(ttl=30, show_spinner=False)
def list_saved_facilities():
    token, repo, branch = _gh_config()
    if not token:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{SAVE_DIR}?ref={branch}"
    try:
        resp = requests.get(url, headers=_gh_headers(token), timeout=10)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        items = resp.json()
        return sorted(item["name"][:-5] for item in items if item["name"].endswith(".json"))
    except Exception:
        return None

def load_facility(slug):
    token, repo, branch = _gh_config()
    if not token:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{SAVE_DIR}/{slug}.json?ref={branch}"
    try:
        resp = requests.get(url, headers=_gh_headers(token), timeout=10)
        resp.raise_for_status()
        content = base64.b64decode(resp.json()["content"]).decode("utf-8")
        return json.loads(content)
    except Exception:
        return None

def save_facility(slug, display_name, clarifier_records):
    token, repo, branch = _gh_config()
    if not token:
        return False, "Save/load isn't set up yet. Ask whoever deployed this to add the GitHub connection in app secrets."
    url = f"https://api.github.com/repos/{repo}/contents/{SAVE_DIR}/{slug}.json"
    payload_data = {
        "facility_name": display_name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "clarifiers": clarifier_records,
    }
    content_b64 = base64.b64encode(json.dumps(payload_data, indent=2).encode("utf-8")).decode("utf-8")
    sha = None
    try:
        get_resp = requests.get(f"{url}?ref={branch}", headers=_gh_headers(token), timeout=10)
        if get_resp.status_code == 200:
            sha = get_resp.json()["sha"]
    except Exception:
        pass
    put_payload = {
        "message": f"Update saved clarifier config for {display_name}",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        put_payload["sha"] = sha
    try:
        put_resp = requests.put(url, headers=_gh_headers(token), json=put_payload, timeout=10)
        put_resp.raise_for_status()
        return True, f"Saved {display_name}."
    except Exception as e:
        return False, f"Save failed: {e}"

# ----------------------------- header ---------------------------------------

logo_light_path = os.path.join(os.path.dirname(__file__), "inframark_logo.webp")
logo_dark_path = os.path.join(os.path.dirname(__file__), "inframark_logo_dark.webp")

theme_type = None
try:
    theme_type = st.context.theme.type
except Exception:
    pass
logo_path = logo_dark_path if theme_type == "dark" else logo_light_path
if not os.path.exists(logo_path):
    logo_path = logo_light_path

h_title, h_logo = st.columns([4, 1])
with h_title:
    st.title("Clarifier State-Point & Solids Flux Analysis")
    st.caption(
        "Enter your clarifier inventory and current plant data, toggle units online/offline, "
        "and see whether the secondary clarification system has capacity, in real time."
    )
with h_logo:
    if os.path.exists(logo_path):
        st.image(logo_path, width=160)
    else:
        st.caption(f"Logo not found at: `{logo_path}`")

st.markdown(f'<span class="req-note">Fields marked <span style="color:red;">*</span> are required '
            f'and drive the analysis. Keep them current with your latest readings.</span>',
            unsafe_allow_html=True)

with st.expander("How this works (read this once)", expanded=False):
    st.markdown(
        """
This tool uses the **state-point / solids flux method** for secondary clarifier analysis.

- **Applied flux** is the total solids load hitting the clarifier surface: `(Q + QR) x MLSS / online area`.
- The **gravity (Vesilind) curve** describes how fast solids can settle at a given concentration, `Vs = Vo x e^(-k x X)`.
- The **underflow requirement** is checked point-by-point between your MLSS and your RAS concentration (Xu).
  If at any concentration in that range the gravity curve can't move solids down as fast as the underflow
  line requires, the blanket will build there. That's an overload, even if the headline numbers look fine.
- **Xu (RAS concentration)** defaults to a simplified mass balance, `Xu = MLSS x (Q+QR) / QR`, assuming
  negligible waste flow relative to recycle. Use a measured RAS TSS value instead if you have one, it's more accurate.

This is a screening tool, not a substitute for engineering judgment on plants near their limits, elevated
sludge blanket depths, poor settleability (bulking/rising sludge), or SVI trending upward.
        """
    )

# ----------------------------- Facility save/load ----------------------------

CLARIFIER_SAVE_COLS = ["Name", "Shape", "Diameter (ft)", "Length (ft)", "Width (ft)"]

def _records_equal(a_records, b_records):
    """NaN-safe comparison of two clarifier record lists. Plain dict/JSON equality
    breaks on NaN (NaN != NaN in Python), which would cause a false 'unsaved changes'
    warning immediately after every successful save or load. pandas' .equals() treats
    NaN as equal to NaN, so route the comparison through DataFrames instead."""
    if a_records is None or b_records is None:
        return False
    try:
        df_a = pd.DataFrame(a_records)
        df_b = pd.DataFrame(b_records)
        for c in CLARIFIER_SAVE_COLS:
            if c not in df_a.columns:
                df_a[c] = None
            if c not in df_b.columns:
                df_b[c] = None
        df_a = df_a[CLARIFIER_SAVE_COLS].reset_index(drop=True)
        df_b = df_b[CLARIFIER_SAVE_COLS].reset_index(drop=True)
        for c in ["Diameter (ft)", "Length (ft)", "Width (ft)"]:
            df_a[c] = pd.to_numeric(df_a[c], errors="coerce")
            df_b[c] = pd.to_numeric(df_b[c], errors="coerce")
        if len(df_a) != len(df_b):
            return False
        return df_a.equals(df_b)
    except Exception:
        return False

st.header("Facility")

if "facility_name" not in st.session_state:
    st.session_state["facility_name"] = ""
if "last_saved_snapshot" not in st.session_state:
    st.session_state["last_saved_snapshot"] = None
if "confirm_overwrite_slug" not in st.session_state:
    st.session_state["confirm_overwrite_slug"] = None

with st.container(border=True):
    facility_name = st.text_input(
        "Facility / Site Name",
        value=st.session_state["facility_name"],
        placeholder="e.g. Hull WPCF",
        help="Used to save and load this site's clarifier setup. If someone else already entered "
             "this site's tanks, load it here first instead of re-typing dimensions.",
        key="input_facility_name",
    )
    if facility_name != st.session_state["facility_name"]:
        st.session_state["confirm_overwrite_slug"] = None  # reset confirm state if they switch sites
    st.session_state["facility_name"] = facility_name

    if not save_load_configured():
        st.caption("Save/load isn't set up yet for this deployment. See README for the one-time GitHub setup.")
    else:
        saved_list = list_saved_facilities()
        load_col, btn_col = st.columns([3, 1])
        with load_col:
            if saved_list is None:
                st.caption("Couldn't reach GitHub to list saved facilities. Check the connection/secrets.")
                chosen = None
            elif not saved_list:
                st.caption("No saved facilities yet, be the first for your site. Enter clarifiers below, "
                           "you'll get a chance to save right after.")
                chosen = None
            else:
                chosen = st.selectbox("Load a previously saved facility", ["-- select --"] + saved_list,
                                       label_visibility="collapsed")
        with btn_col:
            if saved_list and chosen and chosen != "-- select --":
                if st.button("\U0001F4C2 Load"):
                    data = load_facility(chosen)
                    if data:
                        loaded_df = pd.DataFrame(data["clarifiers"])
                        loaded_df["Online?"] = True
                        loaded_df["Flow Split Override (%)"] = None
                        st.session_state.clarifier_df = loaded_df
                        if "clarifier_editor" in st.session_state:
                            del st.session_state["clarifier_editor"]
                        st.session_state["facility_name"] = data.get("facility_name", chosen)
                        st.session_state["last_saved_snapshot"] = data.get("clarifiers")
                        st.session_state["confirm_overwrite_slug"] = None
                        saved_when = data.get("saved_at", "")[:10]
                        st.success(f"Loaded {data.get('facility_name', chosen)}"
                                   + (f" (saved {saved_when})" if saved_when else ""))
                        st.rerun()
                    else:
                        st.error("Couldn't load that facility.")

    if "confirm_clear_form" not in st.session_state:
        st.session_state["confirm_clear_form"] = False

# ----------------------------- Step 1: inputs --------------------------------

st.header("Step 1: Plant & Settling Data")

col_left, col_right = st.columns([1, 1])

with col_left:
    with st.container(border=True):
        st.subheader("Plant Operating Data")
        Q = st.number_input(req("Influent flow, Q (MGD)"), min_value=0.0, value=5.0, step=0.1,
                             help="Current influent flow to the secondary process, in million gallons per day.",
                             key="input_Q")
        QR = st.number_input(req("RAS flow, QR (MGD)"), min_value=0.0, value=2.5, step=0.1,
                              help="Return activated sludge flow rate, in million gallons per day.",
                              key="input_QR")
        MLSS = st.number_input(req("MLSS at clarifier inlet (mg/L)"), min_value=0.0, value=3000.0, step=50.0,
                                help="Mixed liquor suspended solids concentration entering the clarifiers.",
                                key="input_MLSS")

        xu_mode = st.radio(
            "RAS concentration (Xu)",
            ["Estimate from mass balance", "Use measured value"],
            horizontal=True,
            key="input_xu_mode",
        )
        measured_Xu = None
        if xu_mode == "Use measured value":
            measured_Xu = st.number_input(req("Measured RAS TSS (mg/L)"), min_value=0.0, value=9000.0, step=100.0,
                                           help="Lab or probe reading of RAS solids concentration, if available.",
                                           key="input_measured_Xu")

        if QR <= 0 and not measured_Xu:
            st.warning("RAS flow is 0. Enter a measured RAS TSS value above, or set RAS flow > 0, "
                       "to run the analysis.")
            st.stop()

with col_right:
    with st.container(border=True):
        st.subheader("Vesilind Settling Parameters")
        st.caption("Typical ranges: Vo 8-20 ft/hr, k 0.3-0.6 L/g. Refine with your own settling column data if available.")
        Vo = st.number_input(req("Vo (ft/hr)"), min_value=0.1, value=15.0, step=0.5,
                              help="Maximum settling velocity coefficient from the Vesilind model.",
                              key="input_Vo")
        k = st.number_input(req("k (L/g)"), min_value=0.01, value=0.40, step=0.01,
                             help="Concentration coefficient from the Vesilind model.",
                             key="input_k")

# ----------------------------- Step 2: inventory ------------------------------

st.header("Step 2: Clarifier Inventory")
st.caption(
    "Add every clarifier that physically exists at this site, even the ones currently offline. "
    "Toggle Online to run what-if scenarios. Leave Flow Split blank for an even split across online units."
)

if "clarifier_df" not in st.session_state:
    st.session_state.clarifier_df = default_clarifier_template()

edited_df = st.data_editor(
    st.session_state.clarifier_df,
    num_rows="dynamic",
    width='stretch',
    column_config={
        "Shape": st.column_config.SelectboxColumn(options=["Circular", "Rectangular"]),
        "Online?": st.column_config.CheckboxColumn(),
        "Diameter (ft)": st.column_config.NumberColumn(min_value=0.0),
        "Length (ft)": st.column_config.NumberColumn(min_value=0.0),
        "Width (ft)": st.column_config.NumberColumn(min_value=0.0),
        "Flow Split Override (%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0),
    },
    key="clarifier_editor",
)
st.session_state.clarifier_df = edited_df

df = edited_df.copy()
df = df.dropna(subset=["Name"])
if df.empty:
    st.warning("Add at least one clarifier above to run the analysis.")
    st.stop()

df["Area (ft2)"] = df.apply(clarifier_area, axis=1)
online_df = df[df["Online?"] == True].copy()

if online_df.empty:
    st.error("No clarifiers are marked online. Toggle at least one on to run the analysis.")
    st.stop()

# ---- save nudge: right after they've entered/edited the inventory, the natural moment to save ----
if save_load_configured():
    with st.container(border=True):
        records = df[CLARIFIER_SAVE_COLS].to_dict(orient="records")
        unsaved = facility_name.strip() and not _records_equal(records, st.session_state["last_saved_snapshot"])

        if not facility_name.strip():
            st.caption("Enter a Facility / Site Name above to save this clarifier setup for next time.")
        else:
            slug = _slugify(facility_name)
            saved_list_now = list_saved_facilities()
            already_exists = bool(saved_list_now and slug in saved_list_now)
            pending_confirm = st.session_state["confirm_overwrite_slug"] == slug

            if unsaved:
                st.markdown("\u26A0\uFE0F **Unsaved changes.** Nothing here saves automatically, "
                            "closing the tab loses this.")
            else:
                st.caption(f"'{facility_name.strip()}' is saved and up to date.")

            if pending_confirm:
                st.warning(f"'{facility_name.strip()}' already has saved data. "
                           f"Click the button below again to overwrite it.")

            btn_label = "\u26A0\uFE0F Confirm overwrite" if pending_confirm else "\U0001F4BE Save this facility's clarifier setup"
            if st.button(btn_label, disabled=not unsaved and not pending_confirm):
                if not records:
                    st.warning("Nothing to save yet, add clarifiers above first.")
                elif already_exists and not pending_confirm:
                    st.session_state["confirm_overwrite_slug"] = slug
                    st.rerun()
                else:
                    ok, msg = save_facility(slug, facility_name.strip(), records)
                    st.session_state["confirm_overwrite_slug"] = None
                    if ok:
                        st.session_state["last_saved_snapshot"] = records
                        list_saved_facilities.clear()
                        st.success(msg)
                    else:
                        st.error(msg)

# ---- clear form: lives here (after Step 1 & 2 widgets have already rendered this run) so a
# rerun() triggered by this button can't accidentally garbage-collect Vo/k's state. Streamlit
# clears a keyed widget's session_state if that widget isn't re-touched during a completed run,
# so this control has to sit after everything it needs to leave alone. ----
with st.container(border=True):
    if st.session_state["confirm_clear_form"]:
        st.warning("This clears the facility name, plant data, and clarifier table above "
                   "(Vesilind settings stay put). Click again to confirm.")
    clear_label = "\u26A0\uFE0F Confirm clear" if st.session_state["confirm_clear_form"] else "\U0001F9F9 Clear form"
    if st.button(clear_label, key="btn_clear_form",
                 help="Resets facility name, plant data, and the clarifier table for the next scenario or "
                      "the next person. Vo and k are left alone."):
        if not st.session_state["confirm_clear_form"]:
            st.session_state["confirm_clear_form"] = True
            st.rerun()
        else:
            for key in ["input_facility_name", "input_Q", "input_QR", "input_MLSS",
                        "input_xu_mode", "input_measured_Xu", "clarifier_editor"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.session_state["facility_name"] = ""
            st.session_state["clarifier_df"] = default_clarifier_template()
            st.session_state["last_saved_snapshot"] = None
            st.session_state["confirm_overwrite_slug"] = None
            st.session_state["confirm_clear_form"] = False
            st.rerun()

# ---- flow split resolution ----
overrides = online_df["Flow Split Override (%)"]
has_override = overrides.notna().any()
if has_override:
    filled = overrides.fillna(0.0)
    total_pct = filled.sum()
    if total_pct <= 0:
        online_df["Flow % "] = 100.0 / len(online_df)
    else:
        online_df["Flow % "] = filled / total_pct * 100.0
        if abs(total_pct - 100.0) > 1.0:
            st.info(f"Flow split overrides summed to {total_pct:.0f}%, normalized to 100% automatically.")
else:
    online_df["Flow % "] = 100.0 / len(online_df)

A_total = online_df["Area (ft2)"].sum()

# ---- system-level state point (shared by all sections below) ----
Xu = underflow_conc(MLSS, Q, QR, measured_Xu)
SFa_system = applied_flux(Q + QR, MLSS, A_total)
ok_system, margin_system, Xs_curve, grav_curve, line_curve = check_capacity(
    MLSS, Xu, QR, A_total, Vo, k, applied_flux_ref=SFa_system)

# ----------------------------- Step 3: system result --------------------------

st.header("Step 3: System Result")

if ok_system:
    headline = f"Adequate solids flux capacity at this operating point ({margin_system:+.0f}% margin)."
else:
    headline = "The sludge blanket will build up rather than reach steady state at this operating point."
status_banner(ok_system, margin_system, headline)

mlss_threshold, threshold_found = mlss_threshold_from_current(Q, QR, A_total, Vo, k, MLSS, ok_system, measured_Xu)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Online area", f"{A_total:,.0f} ft2", f"{len(online_df)} of {len(df)} units")
m2.metric("Applied flux", f"{SFa_system:.1f} lb/day/ft2")
m3.metric("RAS conc. (Xu)", f"{Xu:,.0f} mg/L")
if ok_system:
    mlss_delta = mlss_threshold - MLSS
    m4.metric("MLSS headroom", f"{'>' if not threshold_found else ''}{mlss_threshold:,.0f} mg/L",
              f"+{mlss_delta:,.0f} mg/L before overload")
else:
    over_by = MLSS - mlss_threshold
    m4.metric("MLSS over limit by", f"{over_by:,.0f} mg/L", f"limit ~{mlss_threshold:,.0f} mg/L", delta_color="inverse")

st.caption(
    f"In plain terms: with this configuration, MLSS could run up to about **{mlss_threshold:,.0f} mg/L** "
    f"before this clarifier system loses capacity (currently **{MLSS:,.0f} mg/L**). "
    f"That's holding flow, RAS rate, and online area fixed at what's entered above. "
    f"(Technical detail: the underlying capacity margin is {margin_system:+.0f}% at the tightest point "
    f"on the flux curve, see the diagram at the bottom of the page.)"
)

# ----------------------------- Step 4: per-clarifier check ---------------------

st.header("Step 4: Per-Clarifier Check")
st.caption("Even if the system average looks fine, an uneven flow split can quietly overload one unit.")

rows = []
for _, r in online_df.iterrows():
    area_i = r["Area (ft2)"]
    q_i = (Q + QR) * r["Flow % "] / 100.0
    sfa_i = applied_flux(q_i, MLSS, area_i)
    ok_i, margin_i, *_ = check_capacity(MLSS, Xu, QR * r["Flow % "] / 100.0, area_i, Vo, k,
                                         applied_flux_ref=sfa_i)
    rows.append({
        "Clarifier": r["Name"],
        "Area (ft2)": round(area_i),
        "Flow %": round(r["Flow % "], 1),
        "Applied Flux (lb/d/ft2)": round(sfa_i, 1),
        "Status": "OK" if ok_i else "OVERLOADED",
        "Margin": f"{margin_i:+.0f}%",
    })
per_df = pd.DataFrame(rows)
st.dataframe(per_df, width='stretch', hide_index=True)

if (per_df["Status"] == "OVERLOADED").any():
    bad = per_df[per_df["Status"] == "OVERLOADED"]["Clarifier"].tolist()
    st.warning(f"Individually overloaded even if the system total isn't: {', '.join(bad)}. "
               f"Check flow split, weir levelness, or gate throttling on those units.")

# ----------------------------- Step 5: what-if recommendations -----------------

st.header("Step 5: What-If Capacity Recommendations")

a_min = min_required_area(MLSS, Xu, QR, Vo, k)
offline_df = df[df["Online?"] == False].copy()

if a_min is None:
    st.error("No feasible area found up to 300,000 ft2 at this MLSS/QR. Check inputs, this MLSS or QR "
             "may be unrealistic, or Xu may be too low relative to MLSS.")
elif not ok_system:
    deficit = a_min - A_total
    st.error(f"Need at least **{a_min:,.0f} ft2** online (currently {A_total:,.0f} ft2, "
             f"a deficit of about **{deficit:,.0f} ft2**).")
    if not offline_df.empty:
        offline_sorted = offline_df.sort_values("Area (ft2)", ascending=False)
        st.write("Offline clarifiers available to bring online, largest first:")
        st.dataframe(
            offline_sorted[["Name", "Shape", "Area (ft2)"]].reset_index(drop=True),
            width='stretch', hide_index=True
        )
        running = A_total
        needed = []
        for _, r in offline_sorted.iterrows():
            if running >= a_min:
                break
            running += r["Area (ft2)"]
            needed.append(r["Name"])
        if running >= a_min:
            st.info(f"Bringing online: **{', '.join(needed)}** would cover the deficit "
                    f"(new total ~{running:,.0f} ft2).")
        else:
            st.warning("Even bringing every offline clarifier online isn't enough at this MLSS/RAS flow. "
                       "Consider reducing MLSS, increasing RAS rate, or both.")
    else:
        st.warning("No offline clarifiers available to add. Consider reducing MLSS or increasing RAS flow.")
else:
    headroom = A_total - a_min
    st.success(f"Headroom: about **{headroom:,.0f} ft2** more than the minimum required "
               f"({a_min:,.0f} ft2 minimum vs {A_total:,.0f} ft2 online).")
    removable = online_df[online_df["Area (ft2)"] <= headroom].sort_values("Area (ft2)")
    if not removable.empty:
        st.write("Individual online units that could be taken offline (e.g. for maintenance) and still stay within capacity:")
        st.dataframe(
            removable[["Name", "Area (ft2)"]].reset_index(drop=True),
            width='stretch', hide_index=True
        )
        st.caption("This checks removing one unit at a time against total headroom. "
                   "Re-run after taking a unit offline to confirm before removing a second.")
    else:
        st.caption("Headroom exists but isn't enough to take any single online unit fully offline.")

# ----------------------------- state point diagram (bottom) --------------------

st.divider()
st.header("State-Point Diagram")
st.caption("Technical detail behind the Step 3 result, for anyone who wants to see the underlying curve.")

fig, ax = plt.subplots(figsize=(7, 5))
X_plot = np.linspace(1, max(Xu * 1.1, MLSS * 1.2), 300)
ax.plot(X_plot, gravity_flux(X_plot, Vo, k), label="Gravity (Vesilind) flux curve", color=BLUE, linewidth=2)
if Xs_curve is not None:
    ax.axvspan(MLSS, Xu, color=GRAY, alpha=0.08, zorder=0)
    ax.plot(Xs_curve, line_curve, label="Underflow requirement", color=FAIL_RED, linestyle="--", linewidth=2.5)
ax.scatter([MLSS], [SFa_system], color=NAVY, zorder=5, s=70, label="State point (applied)")
ax.axvline(Xu, color="gray", linestyle=":", linewidth=1)
ax.text(Xu, ax.get_ylim()[1] * 0.02, " Xu", va="bottom", ha="left", color="gray")
ax.set_xlabel("Solids concentration, X (mg/L)")
ax.set_ylabel("Solids flux (lb/day/ft2)")
ax.set_title("State-Point Diagram")
ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=8, frameon=False)
ax.grid(alpha=0.3)
st.pyplot(fig, width='stretch')

st.caption(
    "The shaded band is the range actually checked, between your MLSS and Xu. The state point "
    "(black dot) can sit above the blue curve and still be fine, that dot isn't the pass/fail test. "
    "What matters is whether the red dashed line stays under the blue curve across the shaded band. "
    "When RAS flow is large relative to plant flow, most solids transport happens via bulk downward "
    "flow from the RAS withdrawal itself, so gravity settling doesn't need to carry much, which is "
    "why the red line can sit well below the blue curve even when the state point sits well above it."
)

st.divider()
st.caption(
    "Built for internal use across all sites. Simplified mass-balance Xu assumes negligible waste flow "
    "relative to RAS recycle; use a measured RAS TSS value when available for a more accurate result. "
    "This is a screening tool, verify against site-specific settling data and engineering judgment before "
    "making operational changes."
)
