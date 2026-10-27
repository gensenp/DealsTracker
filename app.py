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

import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

from DealsTracker import find_clothing_stores, collect_store_deals
import storeLocations

st.set_page_config(page_title="DealsTracker", page_icon="🛍️", layout="wide")

st.title("🛍️ DealsTracker")
st.caption("Find clothing stores in a city and scan their sites for deals.")

RADIUS_CHOICES = [1, 5, 10, 25]


def has_deals(result):
    """True if a scan found anything worth showing."""
    summary = result["summary"]
    return not (summary.startswith("Error:") or "no deals found" in summary.lower())


def discount_pct(item):
    return round((1 - item["price"] / item["original"]) * 100)


def best_discount(result):
    """Deepest markdown percentage in a scan, or 0."""
    return max((discount_pct(i) for i in result["items"]), default=0)


def popup_html(store, result):
    """Marker popup: who the store is, and what is on sale there."""
    rows = ""
    for item in sorted(result["items"], key=discount_pct, reverse=True)[:5]:
        rows += (f"<tr><td style='padding-right:8px'>{item['name'][:40]}</td>"
                 f"<td style='white-space:nowrap'><b>${item['price']:.2f}</b> "
                 f"<s style='color:#888'>${item['original']:.2f}</s> "
                 f"<span style='color:#c0392b'>-{discount_pct(item)}%</span></td></tr>")
    body = f"<table style='font-size:12px'>{rows}</table>" if rows else ""
    promos = f"<div style='color:#c0392b;font-size:12px'>{result['promos']}</div>" if result["promos"] else ""
    if not body and not promos:
        body = "<div style='font-size:12px;color:#888'>No deals found</div>"
    return (f"<div style='min-width:240px'>"
            f"<b>{store['name']}</b><br>"
            f"<span style='font-size:11px;color:#666'>{store.get('address','')}</span>"
            f"<hr style='margin:4px 0'>{promos}{body}"
            f"<a href='{store['website']}' target='_blank' style='font-size:11px'>Visit site</a>"
            f"</div>")


def build_map(stores, origin, radius):
    """Folium map of `stores`, clustered, with the search origin marked."""
    if origin:
        center = origin
    elif stores:
        center = (stores[0]["lat"], stores[0]["lon"])
    else:
        center = (39.8, -98.6)  # continental US, so an empty map isn't blank ocean
    # Plain OSM tiles: folium's CartoDB basemaps now require an API key and
    # render blank without one.
    fmap = folium.Map(location=center, zoom_start=12 if origin else 11,
                      tiles="OpenStreetMap")
    if origin:
        folium.Marker(origin, tooltip="You are here",
                      icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(fmap)
        folium.Circle(origin, radius=radius * 1609.34, color="#3186cc",
                      fill=True, fill_opacity=0.06, weight=1).add_to(fmap)

    # Clustering keeps a dense shopping district readable instead of rendering
    # a pile of overlapping pins.
    cluster = MarkerCluster().add_to(fmap)
    for store in stores:
        result = store["result"]
        deals = has_deals(result)
        tooltip = store["name"]
        if store.get("distance_miles") is not None:
            tooltip += f" — {store['distance_miles']:.1f} mi"
        folium.Marker(
            (store["lat"], store["lon"]),
            tooltip=tooltip,
            popup=folium.Popup(popup_html(store, result), max_width=320),
            icon=folium.Icon(color="red" if deals else "lightgray",
                             icon="tag" if deals else "times", prefix="fa"),
        ).add_to(cluster)

    if stores and not origin:
        fmap.fit_bounds([[s["lat"], s["lon"]] for s in stores])
    return fmap


@st.fragment
def map_section():
    """Location controls, map and nearby-deals list.

    A fragment so that changing the radius or sort order reruns only this
    block. Without it every interaction would rerun the whole script, which
    re-renders (and visibly resets) the map.
    """
    scan = st.session_state.scan
    located, unlocated = scan["located"], scan["unlocated"]

    st.subheader("Deals near you")
    left, mid, right = st.columns([2, 1, 1])
    with left:
        place = st.text_input("Your address or ZIP", key="origin_text",
                              placeholder="e.g. 02119 or 1 City Hall Sq, Boston")
    with mid:
        radius = st.selectbox("Within", RADIUS_CHOICES, index=2,
                              format_func=lambda m: f"{m} miles")
    with right:
        sort_by = st.radio("Sort by", ["Distance", "Discount"], horizontal=True)

    # The browser's own location, if the visitor grants it. Returns a dict with
    # null coordinates until they click and accept, so guard on the values.
    origin = None
    try:
        from streamlit_geolocation import streamlit_geolocation
        st.caption("…or use your device location:")
        fix = streamlit_geolocation()
        if fix and fix.get("latitude") and fix.get("longitude"):
            origin = (fix["latitude"], fix["longitude"])
    except ImportError:
        pass  # optional dependency; typing an address still works

    if place and not origin:
        origin = storeLocations.geocode(place)
        if origin is None:
            st.warning(f"Could not find “{place}”. Try a ZIP or a fuller address.")

    if origin:
        shown = storeLocations.within_radius(located, origin, radius)
        if not shown:
            st.info(f"No stores within {radius} miles. Try a wider radius.")
    else:
        st.info("Set a location above to filter by distance. Showing every store found.")
        shown = located

    if shown or origin:
        # returned_objects=[] stops map panning/zooming from sending state back
        # to Streamlit, which is what otherwise reruns the script and resets the
        # view on every drag.
        st_folium(build_map(shown, origin, radius), key="deals_map",
                  use_container_width=True, height=480, returned_objects=[])

    if sort_by == "Discount":
        shown = sorted(shown, key=lambda s: best_discount(s["result"]), reverse=True)

    st.markdown(f"**{len(shown)} store(s)**"
                + (f" within {radius} miles" if origin else ""))
    for store in shown:
        distance = store.get("distance_miles")
        label = f"{store['name']}" + (f" — {distance:.1f} mi" if distance is not None else "")
        pct = best_discount(store["result"])
        if pct:
            label += f"  ·  up to {pct}% off"
        with st.expander(label, expanded=False):
            if store.get("address"):
                st.caption(store["address"])
            st.markdown(f"[{store['website']}]({store['website']})")
            result = store["result"]
            if result["items"]:
                st.dataframe(
                    [{"Item": i["name"], "Sale": f"${i['price']:.2f}",
                      "Was": f"${i['original']:.2f}", "Off": f"{discount_pct(i)}%"}
                     for i in sorted(result["items"], key=discount_pct, reverse=True)],
                    hide_index=True, width="stretch")
            elif has_deals(result):
                st.success(result["summary"])
            else:
                st.write("🚫 No deals found")

    # Online-only stores have no address to place, so they get their own list
    # rather than being dropped from the results entirely.
    if unlocated:
        st.subheader("Online only / no mappable address")
        for store in unlocated:
            with st.expander(store["name"], expanded=False):
                st.markdown(f"[{store['website']}]({store['website']})")
                result = store["result"]
                if has_deals(result):
                    st.success(result["summary"])
                else:
                    st.write("🚫 No deals found")


# --- Search form ---
with st.form("search"):
    city = st.text_input("City", value="San Jose", placeholder="e.g. San Jose")
    submitted = st.form_submit_button("Search", type="primary")

if submitted:
    if not city.strip():
        st.warning("Enter a city first.")
        st.stop()

    with st.status(f"Searching for clothing stores in {city}…", expanded=True) as status:
        try:
            stores = find_clothing_stores(city.strip())
        except Exception as e:
            status.update(label="Search failed", state="error")
            st.error(str(e))
            st.stop()
        status.update(label=f"Found {len(stores)} store(s) with a website.",
                      state="running")

        if not stores:
            status.update(label="No stores with websites found.", state="error")
            st.stop()

        # Scan every store once, then geocode once. Both results live in session
        # state so that map interactions never trigger a rescan.
        scanned, progress = [], st.progress(0.0)
        for i, store in enumerate(stores, start=1):
            st.write(f"Scanning {store['name']}…")
            scanned.append({**store, "result": collect_store_deals(store)})
            progress.progress(i / len(stores))
        progress.empty()

        st.write("Looking up store locations…")
        located, unlocated = storeLocations.locate_stores(scanned)
        st.session_state.scan = {"city": city, "located": located,
                                 "unlocated": unlocated}
        status.update(label=f"Scanned {len(stores)} store(s) — "
                            f"{len(located)} mapped, {len(unlocated)} online-only.",
                      state="complete")

if "scan" in st.session_state:
    map_section()
