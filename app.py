import streamlit as st
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os

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

def check_capacity(Xa_mgL, Xu_mgL, QR_mgd, area_ft2, Vo, k, n=400):
    """Direct check: does the required underflow removal line stay at/below
    the gravity flux curve for all X between Xa and Xu?"""
    if area_ft2 <= 0 or Xu_mgL <= Xa_mgL:
        return False, -100.0, None, None, None
    QR_gpd_per_ft2 = QR_mgd * 1e6 / area_ft2
    Xs = np.linspace(Xa_mgL, Xu_mgL, n)
    gravity = gravity_flux(Xs, Vo, k)
    line = (Xu_mgL - Xs) * QR_gpd_per_ft2 * 8.34 / 1e6
    gap = gravity - line
    ok = bool(np.min(gap) >= 0)
    worst_idx = int(np.argmin(gap))
    ref = max(line[worst_idx], 1e-6)
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

def max_allowable_mlss(Q_mgd, QR_mgd, area_ft2, Vo, k, measured_Xu=None, mlss_lo=100, mlss_hi=50000, tol=10.0):
    """How high could MLSS go (holding Q, QR, area fixed) before losing capacity.
    If Xu is measured/fixed, Xu stays fixed as MLSS climbs. If Xu is mass-balance
    estimated, it scales up proportionally with MLSS, same as the live calculation."""
    def ok_at(mlss):
        xu = underflow_conc(mlss, Q_mgd, QR_mgd, measured_Xu)
        ok, *_ = check_capacity(mlss, xu, QR_mgd, area_ft2, Vo, k)
        return ok

    if not ok_at(mlss_lo):
        return mlss_lo, True  # already over capacity at a low reference MLSS
    if ok_at(mlss_hi):
        return mlss_hi, False  # capacity extends beyond the search ceiling, report as a floor

    lo, hi = mlss_lo, mlss_hi
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if ok_at(mid):
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

# ----------------------------- Step 1: inputs --------------------------------

st.header("Step 1: Plant & Settling Data")

col_left, col_right = st.columns([1, 1])

with col_left:
    with st.container(border=True):
        st.subheader("Plant Operating Data")
        Q = st.number_input(req("Influent flow, Q (MGD)"), min_value=0.0, value=5.0, step=0.1,
                             help="Current influent flow to the secondary process, in million gallons per day.")
        QR = st.number_input(req("RAS flow, QR (MGD)"), min_value=0.0, value=2.5, step=0.1,
                              help="Return activated sludge flow rate, in million gallons per day.")
        MLSS = st.number_input(req("MLSS at clarifier inlet (mg/L)"), min_value=0.0, value=3000.0, step=50.0,
                                help="Mixed liquor suspended solids concentration entering the clarifiers.")

        xu_mode = st.radio(
            "RAS concentration (Xu)",
            ["Estimate from mass balance", "Use measured value"],
            horizontal=True,
        )
        measured_Xu = None
        if xu_mode == "Use measured value":
            measured_Xu = st.number_input(req("Measured RAS TSS (mg/L)"), min_value=0.0, value=9000.0, step=100.0,
                                           help="Lab or probe reading of RAS solids concentration, if available.")

        if QR <= 0 and not measured_Xu:
            st.warning("RAS flow is 0. Enter a measured RAS TSS value above, or set RAS flow > 0, "
                       "to run the analysis.")
            st.stop()

with col_right:
    with st.container(border=True):
        st.subheader("Vesilind Settling Parameters")
        st.caption("Typical ranges: Vo 8-20 ft/hr, k 0.3-0.6 L/g. Refine with your own settling column data if available.")
        Vo = st.number_input(req("Vo (ft/hr)"), min_value=0.1, value=15.0, step=0.5,
                              help="Maximum settling velocity coefficient from the Vesilind model.")
        k = st.number_input(req("k (L/g)"), min_value=0.01, value=0.40, step=0.01,
                             help="Concentration coefficient from the Vesilind model.")

# ----------------------------- Step 2: inventory ------------------------------

st.header("Step 2: Clarifier Inventory")
st.caption(
    "Add every clarifier that physically exists at this site, even the ones currently offline. "
    "Toggle Online to run what-if scenarios. Leave Flow Split blank for an even split across online units."
)

if "clarifier_df" not in st.session_state:
    st.session_state.clarifier_df = pd.DataFrame(
        [
            {"Name": "Clarifier 1", "Shape": "Circular", "Diameter (ft)": 80.0,
             "Length (ft)": None, "Width (ft)": None, "Online?": True, "Flow Split Override (%)": None},
            {"Name": "Clarifier 2", "Shape": "Circular", "Diameter (ft)": 80.0,
             "Length (ft)": None, "Width (ft)": None, "Online?": True, "Flow Split Override (%)": None},
        ]
    )

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
ok_system, margin_system, Xs_curve, grav_curve, line_curve = check_capacity(MLSS, Xu, QR, A_total, Vo, k)

# ----------------------------- Step 3: system result --------------------------

st.header("Step 3: System Result")

if ok_system:
    headline = f"Adequate solids flux capacity at this operating point ({margin_system:+.0f}% margin)."
else:
    headline = "The sludge blanket will build up rather than reach steady state at this operating point."
status_banner(ok_system, margin_system, headline)

max_mlss, max_mlss_is_exact = max_allowable_mlss(Q, QR, A_total, Vo, k, measured_Xu)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Online area", f"{A_total:,.0f} ft2", f"{len(online_df)} of {len(df)} units")
m2.metric("Applied flux", f"{SFa_system:.1f} lb/day/ft2")
m3.metric("RAS conc. (Xu)", f"{Xu:,.0f} mg/L")
if ok_system:
    mlss_delta = max_mlss - MLSS
    m4.metric("MLSS headroom", f"{'>' if not max_mlss_is_exact else ''}{max_mlss:,.0f} mg/L",
              f"+{mlss_delta:,.0f} mg/L before overload")
else:
    over_by = MLSS - max_mlss
    m4.metric("MLSS over limit by", f"{over_by:,.0f} mg/L", f"limit ~{max_mlss:,.0f} mg/L", delta_color="inverse")

st.caption(
    f"In plain terms: with this configuration, MLSS could run up to about **{max_mlss:,.0f} mg/L** "
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
    ok_i, margin_i, *_ = check_capacity(MLSS, Xu, QR * r["Flow % "] / 100.0, area_i, Vo, k)
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

fig, ax = plt.subplots(figsize=(7, 4.5))
X_plot = np.linspace(1, max(Xu * 1.1, MLSS * 1.2), 300)
ax.plot(X_plot, gravity_flux(X_plot, Vo, k), label="Gravity (Vesilind) flux curve", color=BLUE)
if Xs_curve is not None:
    ax.plot(Xs_curve, line_curve, label="Underflow requirement", color=FAIL_RED, linestyle="--")
ax.scatter([MLSS], [SFa_system], color=NAVY, zorder=5, label="State point (applied)")
ax.axvline(Xu, color="gray", linestyle=":", linewidth=1)
ax.text(Xu, ax.get_ylim()[1] * 0.02, " Xu", va="bottom", ha="left", color="gray")
ax.set_xlabel("Solids concentration, X (mg/L)")
ax.set_ylabel("Solids flux (lb/day/ft2)")
ax.set_title("State-Point Diagram")
ax.legend(loc="upper right", fontsize=8)
ax.grid(alpha=0.3)
st.pyplot(fig, width='stretch')

st.divider()
st.caption(
    "Built for internal use across all sites. Simplified mass-balance Xu assumes negligible waste flow "
    "relative to RAS recycle; use a measured RAS TSS value when available for a more accurate result. "
    "This is a screening tool, verify against site-specific settling data and engineering judgment before "
    "making operational changes."
)
