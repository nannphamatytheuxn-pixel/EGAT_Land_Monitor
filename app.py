import streamlit as st
import ee
import folium
import json
import geopandas as gpd
import tempfile
import zipfile
import os
from folium.plugins import Draw
from streamlit_folium import st_folium

PROJECT_ID = "gee-learning-personal"

PROJECT_ID = "gee-learning-personal"

def has_service_account_secret():
    try:
        return "gcp_service_account" in st.secrets
    except Exception:
        return False

def init_earth_engine():
    if has_service_account_secret():
        service_account_info = dict(st.secrets["gcp_service_account"])
        credentials = ee.ServiceAccountCredentials(
            service_account_info["client_email"], key_data=json.dumps(service_account_info)
        )
        ee.Initialize(credentials, project=PROJECT_ID)
    else:
        try:
            ee.Initialize(project=PROJECT_ID)
        except Exception:
            ee.Authenticate()
            ee.Initialize(project=PROJECT_ID)

st.title("ระบบติดตามการลุกล้ำพื้นที่ การไฟฟ้าฝ่ายผลิตแห่งประเทศไทย")

st.header("กำหนดช่วงเวลาวิเคราะห์")
col1, col2 = st.columns(2)
with col1:
    st.subheader("ช่วงเวลาอ้างอิง (ก่อนการเปลี่ยนแปลง)")
    t1_start = st.date_input("วันเริ่มต้น", key="t1_start")
    t1_end = st.date_input("วันสิ้นสุด", key="t1_end")
with col2:
    st.subheader("ช่วงเวลาเปรียบเทียบ (หลังการเปลี่ยนแปลง)")
    t2_start = st.date_input("วันเริ่มต้น", key="t2_start")
    t2_end = st.date_input("วันสิ้นสุด", key="t2_end")

st.header("กำหนดขอบเขตพื้นที่ศึกษา (AOI)")
aoi_method = st.radio("วิธีเลือกพื้นที่", options=["draw", "upload"],
    format_func=lambda x: "วาดบนแผนที่" if x == "draw" else "อัปโหลด GeoJSON")
aoi_geojson = None

if aoi_method == "draw":
    m = folium.Map(location=[13.7563, 100.5018], zoom_start=6)
    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr="Google", name="Google Satellite",
    ).add_to(m)
    Draw(export=False).add_to(m)
    output = st_folium(m, width=700, height=500)
    if output and output.get("last_active_drawing"):
        aoi_geojson = output.get("last_active_drawing")
        st.success("วาดพื้นที่สำเร็จ")
else:
    uploaded_file = st.file_uploader("อัปโหลดไฟล์ GeoJSON", type=["geojson", "json"])
    if uploaded_file is not None:
        aoi_geojson = json.load(uploaded_file)
        st.success("อัปโหลดไฟล์สำเร็จ")
        st.json(aoi_geojson)

if "result_map" not in st.session_state:
    st.session_state.result_map = None

st.header("วิเคราะห์การเปลี่ยนแปลงพื้นที่")
run_button = st.button("เริ่มวิเคราะห์")

if run_button:
    if aoi_geojson is None:
        st.error("กรุณาวาดพื้นที่หรืออัปโหลดไฟล์ GeoJSON ก่อนวิเคราะห์")
    else:
        with st.spinner("กำลังวิเคราะห์ข้อมูลดาวเทียม..."):
            geometry = aoi_geojson["geometry"]
            geom_type = geometry["type"]
            coords = geometry["coordinates"]

            if geom_type == "Polygon":
                aoi = ee.Geometry.Polygon(coords)
            else:
                aoi = ee.Geometry(aoi_geojson)

            def get_sentinel2(start_date, end_date, region):
                return (
                    ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                    .filterBounds(region)
                    .filterDate(str(start_date), str(end_date))
                    .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
                    .median()
                    .clip(region)
                )

            img_t1 = get_sentinel2(t1_start, t1_end, aoi)
            img_t2 = get_sentinel2(t2_start, t2_end, aoi)

            def calc_indices(img):
                ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
                ndbi = img.normalizedDifference(["B11", "B8"]).rename("NDBI")
                ndwi = img.normalizedDifference(["B3", "B8"]).rename("NDWI")
                return ndvi, ndbi, ndwi

            ndvi_t1, ndbi_t1, ndwi_t1 = calc_indices(img_t1)
            ndvi_t2, ndbi_t2, ndwi_t2 = calc_indices(img_t2)

            # สูตรคำนวณ Delta
            delta_ndbi = ndbi_t2.subtract(ndbi_t1)  # T2 - T1
            delta_ndvi = ndvi_t1.subtract(ndvi_t2)  # T1 - T2

            # เกณฑ์จำแนก
            is_red = delta_ndbi.gt(0.05).And(ndwi_t2.lt(0.15))
            is_yellow = (
                delta_ndvi.gt(0.03)
                .And(ndwi_t2.lt(0.15))
                .And(is_red.Not())
            )

            def get_tile_url(image, vis_params):
                map_id = image.getMapId(vis_params)
                return map_id["tile_fetcher"].url_format

            red_layer = is_red.selfMask().visualize(palette=["FF0000"])
            yellow_layer = is_yellow.selfMask().visualize(palette=["FFFF00"])
            rgb_vis = {"bands": ["B4", "B3", "B2"], "min": 0, "max": 3000}

            red_url = get_tile_url(red_layer, {})
            yellow_url = get_tile_url(yellow_layer, {})
            t1_url = get_tile_url(img_t1, rgb_vis)
            t2_url = get_tile_url(img_t2, rgb_vis)

            center = aoi.centroid().getInfo()["coordinates"]

            st.session_state.result_map = {
                "center": center,
                "red_url": red_url,
                "yellow_url": yellow_url,
                "t1_url": t1_url,
                "t2_url": t2_url,
                "aoi_geojson": aoi_geojson,
                "aoi": aoi,
                "is_red": is_red,
                "is_yellow": is_yellow,
            }
            st.success("วิเคราะห์สำเร็จ!")

if st.session_state.result_map is not None:
    r = st.session_state.result_map

    st.header("ผลการวิเคราะห์การเปลี่ยนแปลงพื้นที่")
    m_result = folium.Map(location=[r["center"][1], r["center"][0]], zoom_start=12)
    folium.TileLayer(
        tiles="https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        attr="Google", name="Google Satellite",
    ).add_to(m_result)
    folium.TileLayer(
        tiles=r["t1_url"], attr="GEE", name="ภาพดาวเทียม T1", overlay=True
    ).add_to(m_result)
    folium.TileLayer(
        tiles=r["t2_url"], attr="GEE", name="ภาพดาวเทียม T2", overlay=True
    ).add_to(m_result)
    folium.TileLayer(
        tiles=r["red_url"], attr="GEE", name="สิ่งปลูกสร้างรุกล้ำ (แดง)", overlay=True
    ).add_to(m_result)
    folium.TileLayer(
        tiles=r["yellow_url"], attr="GEE", name="เกษตรรุกล้ำ (เหลือง)", overlay=True
    ).add_to(m_result)
    folium.GeoJson(
        r["aoi_geojson"],
        name="ขอบเขต AOI",
        style_function=lambda x: {"color": "cyan", "weight": 2, "fillOpacity": 0}
    ).add_to(m_result)
    folium.LayerControl().add_to(m_result)
    st_folium(m_result, width=700, height=500)

    st.header("ดาวน์โหลดผลลัพธ์")
    col1, col2 = st.columns(2)

    def make_shapefile(fc_geojson, filename):
        gdf = gpd.GeoDataFrame.from_features(fc_geojson["features"])
        gdf.crs = "EPSG:4326"
        with tempfile.TemporaryDirectory() as tmpdir:
            shp_path = os.path.join(tmpdir, filename)
            gdf.to_file(shp_path + ".shp")
            zip_path = os.path.join(tmpdir, filename + ".zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for ext in [".shp", ".shx", ".dbf", ".prj"]:
                    zf.write(shp_path + ext, filename + ext)
            with open(zip_path, "rb") as f:
                return f.read()

    with col1:
        try:
            red_fc = ee.FeatureCollection(
                r["is_red"].selfMask().reduceToVectors(
                    geometry=r["aoi"], scale=30,
                    geometryType="polygon", maxPixels=1e9, bestEffort=True
                )
            )
            red_geojson = red_fc.getInfo()
            if red_geojson["features"]:
                data = make_shapefile(red_geojson, "red_encroachment")
                st.download_button(
                    "ดาวน์โหลด สิ่งปลูกสร้างรุกล้ำ (.shp)",
                    data, file_name="red_encroachment.zip",
                    mime="application/zip"
                )
            else:
                st.info("ไม่พบพื้นที่สิ่งปลูกสร้างรุกล้ำ")
        except Exception as e:
            st.error(f"เกิดข้อผิดพลาด: {e}")

    with col2:
        try:
            yellow_fc = ee.FeatureCollection(
                r["is_yellow"].selfMask().reduceToVectors(
                    geometry=r["aoi"], scale=30,
                    geometryType="polygon", maxPixels=1e9, bestEffort=True
                )
            )
            yellow_geojson = yellow_fc.getInfo()
            if yellow_geojson["features"]:
                data = make_shapefile(yellow_geojson, "yellow_encroachment")
                st.download_button(
                    "ดาวน์โหลด เกษตรรุกล้ำ (.shp)",
                    data, file_name="yellow_encroachment.zip",
                    mime="application/zip"
                )
            else:
                st.info("ไม่พบพื้นที่เกษตรรุกล้ำ")
        except Exception as e:
            st.error(f"เกิดข้อผิดพลาด: {e}")