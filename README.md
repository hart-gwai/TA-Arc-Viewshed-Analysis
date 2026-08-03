# Cell Site Data Analyser – User Manual & Guidelines (v2.0.2)

## 1. Overview & Theory
The **Cell Site Data Analyser** is an advanced QGIS plugin designed specifically for locating and tracking mobile phone signals, heavily utilized in Search & Rescue (SAR) and law enforcement operations. 

**The Core Problem:**
Call Detail Records (CDRs) or mobile ping data provide raw geometric probability areas (pie-slice or donut "arcs") based on a tower's coordinates, azimuth, beam width, and estimated distance. However, these theoretical geometric arcs are flawed in the real world:
1. They ignore physical topography (radio signals cannot travel through mountains).
2. They do not automatically account for a target moving over time.

**The Methodology:**
This plugin merges *geometric arc boundaries* with *topographic Line-of-Sight (viewshed) analysis* and *temporal cascade logic*.
1. **Geometry:** It calculates theoretical arc polygons from raw CSV data using angles and Timing Advance distances.
2. **Topography:** It calculates topographic viewsheds from the tower using a Digital Elevation Model (DEM).
3. **Temporal Cascade:** It groups sequential pings over time and mathematically intersects their geometries to find the highest probability core locations.
4. **Synthesis:** It performs bounded raster multiplication, clipping topographic viewsheds perfectly to the cascaded geometric cores.

---

## 2. Prerequisites & Required Layers

### Required QGIS Plugins
*   **Visibility Analysis plugin** (by Zoran Čučković): The Cell Site Data Analyser relies on this plugin's underlying algorithms to generate the initial 360-degree topographic master viewsheds. Ensure it is installed and enabled via `Plugins > Manage and Install Plugins`.

### Required GIS Layers
Before running the tool, you must have the following layers loaded in your QGIS project:
1.  **Digital Elevation Model (DEM):** A raster layer containing elevation data. 
    *   *Critical Requirement:* The DEM must be in a **Projected Coordinate System (CRS)** using meters (e.g., Hong Kong Grid 1980 / EPSG:2326). It will fail if it is in a geographic system (WGS84 / Degrees).
2.  **Building Footprints Layer (Optional but Recommended):** A vector polygon layer of buildings (with a name field like `BuildingNameEN`). Used in Step 1 to automatically assign unnamed cell pings to the physical building they sit on.

---

## 3. CSV Input Format & Requirements
The plugin reads raw CSV tables. The tool features an auto-mapper in Step 1, but your data must provide enough parameters to build a physical wedge shape.

### Required Data Fields
*   **Coordinates:** `Longitude` (X) and `Latitude` (Y). (Generally WGS84).
*   **Time:** `Timestamp`, `Event_Time`, or `DateTime` (e.g., `YYYY-MM-DD HH:MM:SS`).
*   **Distance (Radius in meters / Timing Advance):** `Min of minD` (Minimum Radius) and `Max of maxD` (Maximum Radius). 
    *   *Note:* Timing Advance (TA) allows networks to estimate distance via signal delay. If TA is known, `Min of minD` is > 0, creating a "donut" band. If TA is unknown, `Min of minD` is 0, creating a solid wedge starting from the tower.
*   **Direction (Angle in degrees):** The plugin accepts two formats:
    *   *Format A:* `Start_Azimuth` and `End_Azimuth`.
    *   *Format B:* Center `Azimuth` and `Beam_Width`.

### Optional Data Fields
*   **Cell_Site:** Tower name. If blank, it relies on the Building Layer or generates a fallback ID like `UNLOCATED_{lat}_{lon}`.
*   **Observer_Height:** Antenna height (in meters). Defaults to 1.75m if empty.

---

## 4. Step 1: Prepare Cell Site Data
*Converts the raw CSV into a clean, standardized GIS point layer.*

1. **Save your QGIS project** (The plugin writes files to a scenario folder next to your `.qgs` file).
2. Select your DEM Layer in the main window dropdown.
3. Click **Step 1 — Prepare Cell Site Data**.
4. Browse for your raw CSV. 
5. Verify the auto-mapped columns (ensure X=Longitude, Y=Latitude).
6. *(Optional)* Check "Assign building names from polygon layer", select your building layer, and choose the name field. The tool will do a spatial join to identify tower names.
7. Click **Run**.
*   **Output:** Generates `Prepared_Ping_Layer` (a GeoPackage of points in EPSG:4326).

---

## 5. Step 2: The Core Analysis & Methodology
*This executes the complex temporal, geometric, and topographic math.*

In the main dialog, select your `Prepared_Ping_Layer` and your DEM Layer. 

### A. The Temporal Logic: Rolling Time Window
If the target is moving, you can activate the **Rolling Time Window**.
*   **Window Size (e.g., 3):** The plugin scoops up 3 sequential pings based on time.
*   **Step Size (e.g., 1):** The window slides forward 1 ping at a time.
*   **What it does:** Instead of treating every ping as an isolated event, it dissolves the geometries of pings from the *same tower* within that time window, and assigns them a combined timestamp. This ensures that the subsequent Cascade Logic looks for geographic overlaps *across that entire time block*.

### B. The Geometric Logic: The 3-Tier Cascade Polygons
Once the geometries are built using Azimuths and Timing Advance Distances, the plugin feeds them into the **Cascade Logic Engine**. This mathematically isolates the highest probability location based on 3 strict rules:

*   **Rule 1 (No Overlaps):** If, during a specific timestamp (or time window), the geometric arcs do not touch or overlap at all, the plugin keeps every arc as-is.
*   **Rule 2 (Primary Pairwise Overlap):** If *at least one* overlap exists during a timestamp, the plugin calculates the intersection (the overlapping pocket). **Crucially: Any lone arcs or non-overlapping remnants of the base arcs are completely discarded.** Because the target pinged multiple towers at the same time, they *must* be inside the overlap.
*   **Rule 3 (Mutual Core / Secondary Overlap):** If multiple overlapping pockets from Rule 2 intersect with *each other* (e.g., a 3-way or 4-way tower overlap), the plugin collapses them into a "Mutual Core"—the absolute smallest geometry where all arcs intersect. 

*Result:* This produces the `Overlapped Cell Site Arcs` layer—the precise geometric boundary of the target.

### C. The Topographic Logic: Master Viewsheds
To avoid computing hundreds of viewsheds, the plugin extracts "Unique Cell Sites". For every physical tower, it finds the **Global Maximum Radius** across all your pings, and computes a single **360-degree Master Viewshed** using the DEM out to that exact distance.
*   `1` = Topographically Visible.
*   `NoData` = Blocked by terrain.

### D. The Synthesis: Bounded Raster Multiplication
This is the final step where geometry and topography are combined.
1. The plugin takes the cascaded geometric pocket (from Step B).
2. It uses `GDAL Warp` to crop the 360-degree Master Viewshed down to the exact shape of that pocket.
3. If the pocket was formed by multiple towers (Rule 2 or 3), the plugin takes the cropped viewshed from Tower A and mathematically **multiplies** it by the cropped viewshed of Tower B.
    *   `1 (Visible to A) x 1 (Visible to B) = 1 (Visible to both)`
    *   `1 (Visible to A) x 0 (Blocked from B) = 0 (Excluded)`
4. The final output is written as a compressed GeoTIFF and loaded into the **Combined Viewshed** group.

---

## 6. Interpreting the Legend & Results

Once the process finishes, your QGIS Layers panel will be populated:

1. **Combined Viewshed (Group):** Expand this. Here are your final probability maps, named by timestamp. The colored pixels represent the physical ground that is geographically inside the cascaded intersection **AND** topographically visible to all involved towers. 
    * *Note: If an area is tagged with the suffix `— no line of sight`, it means the topographic terrain completely blocked the signal within the geometric pocket, resulting in an empty viewshed.*
2. **Master Viewshed (Group):** The raw 360-degree topographic layers (collapsed by default).
3. **Overlapped Cell Site Arcs:** The vector polygons showing the results of the 3-Tier Cascade Rules *before* terrain blocking is applied.
4. **Cell Site Arcs:** The raw, un-cascaded base wedges (hidden by default).