import geopandas as gpd
import matplotlib.pyplot as plt

# Load shapefile
gdf = gpd.read_file(INPUT_FILE_PATH)

# Filter to STATE == 'MI'
gdf_mi = gdf[gdf["STATE"] == "MI"]

gdf_mi.head()

# Dissolve on 'CWA'
gdf_dissolved = gdf_mi.dissolve(by="CWA").reset_index()

gdf_dissolved.plot()
plt.show()

# Save to GeoJSON
gdf_dissolved.to_file(OUTPUT_FILE_PATH)
