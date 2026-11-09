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
# Copy any secrets into the environment so the rest of the app (which reads
# os.environ) works unchanged both locally (.env) and when deployed.
try:
    for _k in ("GROQ_API_KEY", "GOOGLE_PLACES_API_KEY", "DATABASE_URL"):
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except Exception:
    pass  # no secrets.toml locally — .env handles it instead

import pipeline
from DealsTracker import build_location
from deals import has_deals
from ui import ITEMS_SHOWN, STATUS_NOTES, pluralize, provenance, render_items

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


def run_search(params, force_keys=frozenset()):
    """Run the pipeline and keep the result for later reruns."""
    city, state, postal_code = params
    status_line = st.empty()

    def on_progress(name, index, total):
        status_line.caption(f"Checking {name} ({index} of {total})…")

    with st.spinner("Searching…"):
        try:
            result = pipeline.search_deals(city, state, postal_code,
                                           force_keys=force_keys,
                                           on_progress=on_progress)
        except Exception as exc:
            status_line.empty()
            st.error(str(exc))
            return
    status_line.empty()
    st.session_state["params"] = params
    st.session_state["result"] = result


def render_chain(chain):
    """One retailer: its deals, its locations, and how current they are."""
    with st.container(border=True):
        header, refresh = st.columns([5, 1])
        with header:
            st.markdown(f"### {chain.name}")
        with refresh:
            if st.button("Refresh", key=f"refresh-{chain.key}",
                         use_container_width=True):
                st.session_state["force_key"] = chain.key
                st.rerun()

        # How current the deals are, and where they came from.
        st.caption(provenance(chain))
        store_count = len(chain.stores)

        if chain.error:
            st.warning(chain.error)
        elif not has_deals(chain.deals):
            icon, note = STATUS_NOTES.get(chain.status, ("🚫", "No deals found."))
            st.write(f"{icon} {note}")
        else:
            for promo in chain.deals.get("promotions") or []:
                st.success(promo)

            items = chain.deals.get("items") or []
            if items:
                lines = render_items(items)
                st.markdown("\n".join(lines[:ITEMS_SHOWN]))
                if len(lines) > ITEMS_SHOWN:
                    with st.expander(f"Show {len(lines) - ITEMS_SHOWN} more"):
                        st.markdown("\n".join(lines[ITEMS_SHOWN:]))

        if chain.sale_url:
            st.markdown(f"[Shop the sale →]({chain.sale_url})")

        with st.expander(pluralize(store_count, "location").capitalize()):
            for store in chain.stores:
                address = store.get("address") or ""
                st.markdown(f"**{store.get('name', '')}**"
                            + (f"  \n{address}" if address else ""))


# --- Page -------------------------------------------------------------------
st.title("🛍️ DealsTracker")
st.caption("Find clothing stores in a city and see what's on sale.")

with st.form("search"):
    # State and ZIP narrow the search when several towns share a city name
    # (Springfield, Portland, Columbus...). Both are optional.
    col_city, col_state, col_zip = st.columns([3, 1.3, 1.5])
    with col_city:
        city = st.text_input("City", value="Boston", placeholder="e.g. Boston")
    with col_state:
        state = st.selectbox("State", US_STATES)
    with col_zip:
        postal_code = st.text_input("ZIP", placeholder="optional")
    submitted = st.form_submit_button("Search", type="primary")

# A Refresh button stores its chain key and reruns; pick that up before drawing.
forced_key = st.session_state.pop("force_key", None)
if forced_key and st.session_state.get("params"):
    run_search(st.session_state["params"], force_keys={forced_key})
elif submitted:
    chosen_state = "" if state == ANY_STATE else state
    if not build_location(city, chosen_state, postal_code):
        st.warning("Enter a city or a ZIP code first.")
        st.stop()
    run_search((city, chosen_state, postal_code))

result = st.session_state.get("result")
if result is None:
    st.stop()

if not result.chains:
    st.info(f"No stores with websites found in {result.location}. "
            "Try a different city, or widen the search by clearing the ZIP.")
    st.stop()

with_deals = sum(1 for c in result.chains if has_deals(c.deals))
st.subheader(f"{with_deals} of {len(result.chains)} retailers have deals")
st.caption(
    f"{result.location} · {result.total_seconds:.0f}s · "
    f"{result.scraped} scraped, {result.cached} from saved results, "
    f"{result.skipped} skipped"
)
st.caption("Deals come from each retailer's website — in-store prices may differ.")

for chain in result.chains:
    render_chain(chain)
