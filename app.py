# This project lives in a OneDrive folder, which rewrites file mtimes on sync.
# That can leave a stale __pycache__/*.pyc that shadows edits to imported modules
# (e.g. DealsTracker), causing ImportErrors after edits. Clear it on startup and
# stop writing new bytecode so we always run the current source.
import sys, os, shutil
sys.dont_write_bytecode = True
shutil.rmtree(os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__"),
              ignore_errors=True)

import streamlit as st

# On Streamlit Cloud there is no .env file; keys come from the app's Secrets.
# Copy any secrets into the environment so DealsTracker (which reads os.environ)
# works unchanged both locally (.env) and when deployed.
try:
    for _k in ("GROQ_API_KEY", "GOOGLE_PLACES_API_KEY"):
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass  # no secrets.toml locally — .env handles it instead

from DealsTracker import find_clothing_stores, get_store_deals, build_location

ANY_STATE = "— Any —"
US_STATES = [
    ANY_STATE,
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]

st.set_page_config(page_title="DealsTracker", page_icon="🛍️", layout="centered")

st.title("🛍️ DealsTracker")
st.caption("Find clothing stores in a city and scan their sites for deals.")

# --- Search form ---
with st.form("search"):
    # State and ZIP narrow the search when several towns share a city name
    # (Springfield, Portland, Columbus...). Both are optional.
    col_city, col_state, col_zip = st.columns([3, 1.3, 1.5])
    with col_city:
        city = st.text_input("City", value="San Jose", placeholder="e.g. San Jose")
    with col_state:
        state = st.selectbox("State", US_STATES)
    with col_zip:
        postal_code = st.text_input("ZIP", placeholder="optional")
    submitted = st.form_submit_button("Search", type="primary")

if submitted:
    state = "" if state == ANY_STATE else state
    location = build_location(city, state, postal_code)

    if not location:
        st.warning("Enter a city or a ZIP code first.")
        st.stop()

    # 1. Look up stores
    with st.status(f"Searching for clothing stores in {location}…", expanded=True) as status:
        try:
            stores = find_clothing_stores(city, state, postal_code)
        except Exception as e:
            status.update(label="Search failed", state="error")
            st.error(str(e))
            st.stop()
        status.update(
            label=f"Found {len(stores)} store(s) with a website.", state="complete"
        )

    if not stores:
        st.info(f"No stores with websites found in {location}. "
                "Try a different city, or widen the search by clearing the ZIP.")
        st.stop()

    # 2. Scan each store's site, streaming results in as they finish
    progress = st.progress(0.0)
    for i, store in enumerate(stores, start=1):
        with st.container(border=True):
            st.markdown(f"### {store['name']}")
            if store["address"]:
                st.caption(store["address"])
            st.markdown(f"[{store['website']}]({store['website']})")

            with st.spinner("Scanning for deals…"):
                deals = get_store_deals(store)

            if deals.startswith("Error:"):
                st.warning(deals)
            elif "no deals found" in deals.lower():
                st.write("🚫 No deals found")
            else:
                st.success(deals)

        progress.progress(i / len(stores))

    progress.empty()
    st.toast("Done!", icon="✅")
