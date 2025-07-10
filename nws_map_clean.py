import geopandas as gpd
import matplotlib.pyplot as plt

# Load shapefile
gdf = gpd.read_file("/Users/grantschwab/Desktop/nws_map/raw/nws_map_raw/z_18mr25.shp")

# Filter to STATE == 'MI'
gdf_mi = gdf[gdf["STATE"] == "MI"]

gdf_mi.head()

# Dissolve on 'CWA'
gdf_dissolved = gdf_mi.dissolve(by="CWA").reset_index()

gdf_dissolved.plot()
plt.show()

# Save to GeoJSON
gdf_dissolved.to_file("/Users/grantschwab/Desktop/nws_map/output/mi_dissolved_by_cwa.geojson", driver="GeoJSON")
