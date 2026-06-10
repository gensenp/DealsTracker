# This project lives in a OneDrive folder, which rewrites file mtimes on sync.
# That can leave a stale __pycache__/*.pyc that shadows edits to imported modules
# (e.g. DealsTracker), causing ImportErrors after edits. Clear it on startup and
# stop writing new bytecode so we always run the current source.
import sys, os, shutil
sys.dont_write_bytecode = True
shutil.rmtree(os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__"),
              ignore_errors=True)

import streamlit as st

from DealsTracker import find_clothing_stores, get_store_deals

st.set_page_config(page_title="DealsTracker", page_icon="🛍️", layout="centered")

st.title("🛍️ DealsTracker")
st.caption("Find clothing stores in a city and scan their sites for deals.")

# --- Search form ---
with st.form("search"):
    city = st.text_input("City", value="San Jose", placeholder="e.g. San Jose")
    submitted = st.form_submit_button("Search", type="primary")

if submitted:
    if not city.strip():
        st.warning("Enter a city first.")
        st.stop()

    # 1. Look up stores
    with st.status(f"Searching for clothing stores in {city}…", expanded=True) as status:
        try:
            stores = find_clothing_stores(city.strip())
        except Exception as e:
            status.update(label="Search failed", state="error")
            st.error(str(e))
            st.stop()
        status.update(
            label=f"Found {len(stores)} store(s) with a website.", state="complete"
        )

    if not stores:
        st.info("No stores with websites found. Try a different city.")
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
