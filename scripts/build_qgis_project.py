#!/usr/bin/env python3
"""
Generate a ready-to-open QGIS project (.qgs) bundling all tram layers.

Produces:
  tartu-tramline.qgs   — open this in QGIS 3.x
  data/tartu_tramline.gpkg — already produced by build_tram_geodata.py

The .qgs file references data/tartu_tramline.gpkg with relative paths,
applies inline styling per layer (matching the .qml files), and adds
Maa-amet WMS basemaps (orthophoto + topographic).

CRS: EPSG:3301 (L-EST97) for project; layers in EPSG:4326 reproject on-fly.
"""

import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).parent.parent
GPKG = "./data/tartu_tramline.gpkg"

# Layer definitions: (id_key, gpkg_layer_name, display_name, geometry_type)
LAYERS = [
    ("routes",          "routes",          "Tram Routes",         "LineString"),
    ("stops",           "stops",           "Tram Stops",          "Point"),
    ("catchments_400m", "catchments_400m", "Stop Catchments 400 m", "Polygon"),
    ("service_area_p1", "service_area_p1", "Phase 1 Service Area", "MultiPolygon"),
    ("corridor_150m",   "corridor_150m",   "150 m Route Corridor", "Polygon"),
]

def lid(key):
    """Stable layer id."""
    return f"{key}_a1b2c3d4e5f6"

# Layer renderers (inline XML)
def renderer_routes():
    return """<renderer-v2 attr="id" type="categorizedSymbol" symbollevels="0" enableorderby="0" forceraster="0">
      <categories>
        <category render="true" symbol="0" value="L1P1" label="Line 1 — Phase 1 (Core, 2028–31)"/>
        <category render="true" symbol="1" value="L1P2" label="Line 1 — Phase 2 (Ränilinn ext.)"/>
        <category render="true" symbol="2" value="L2" label="Line 2 — Ropka (Phase 3)"/>
        <category render="true" symbol="3" value="L3" label="Line 3 — Raadi (Phase 3)"/>
        <category render="true" symbol="4" value="HOLMI_BRIDGE" label="New Holmi Bridge"/>
      </categories>
      <symbols>
        <symbol name="0" type="line" alpha="1" force_rhr="0" clip_to_extent="1">
          <layer pass="0" enabled="1" class="SimpleLine" locked="0">
            <Option type="Map">
              <Option name="capstyle" type="QString" value="round"/>
              <Option name="customdash" type="QString" value="5;2"/>
              <Option name="customdash_unit" type="QString" value="MM"/>
              <Option name="joinstyle" type="QString" value="round"/>
              <Option name="line_color" type="QString" value="230,57,70,255"/>
              <Option name="line_style" type="QString" value="solid"/>
              <Option name="line_width" type="QString" value="1.6"/>
              <Option name="line_width_unit" type="QString" value="MM"/>
              <Option name="use_custom_dash" type="QString" value="0"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="1" type="line" alpha="1" force_rhr="0" clip_to_extent="1">
          <layer pass="0" enabled="1" class="SimpleLine" locked="0">
            <Option type="Map">
              <Option name="capstyle" type="QString" value="round"/>
              <Option name="customdash" type="QString" value="3;1.5"/>
              <Option name="customdash_unit" type="QString" value="MM"/>
              <Option name="joinstyle" type="QString" value="round"/>
              <Option name="line_color" type="QString" value="244,162,97,255"/>
              <Option name="line_style" type="QString" value="dash"/>
              <Option name="line_width" type="QString" value="1.4"/>
              <Option name="line_width_unit" type="QString" value="MM"/>
              <Option name="use_custom_dash" type="QString" value="1"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="2" type="line" alpha="1" force_rhr="0" clip_to_extent="1">
          <layer pass="0" enabled="1" class="SimpleLine" locked="0">
            <Option type="Map">
              <Option name="customdash" type="QString" value="2.5;1.5"/>
              <Option name="customdash_unit" type="QString" value="MM"/>
              <Option name="line_color" type="QString" value="29,78,216,255"/>
              <Option name="line_style" type="QString" value="dash"/>
              <Option name="line_width" type="QString" value="1.2"/>
              <Option name="line_width_unit" type="QString" value="MM"/>
              <Option name="use_custom_dash" type="QString" value="1"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="3" type="line" alpha="1" force_rhr="0" clip_to_extent="1">
          <layer pass="0" enabled="1" class="SimpleLine" locked="0">
            <Option type="Map">
              <Option name="customdash" type="QString" value="2.5;1.5"/>
              <Option name="customdash_unit" type="QString" value="MM"/>
              <Option name="line_color" type="QString" value="124,58,237,255"/>
              <Option name="line_style" type="QString" value="dash"/>
              <Option name="line_width" type="QString" value="1.2"/>
              <Option name="line_width_unit" type="QString" value="MM"/>
              <Option name="use_custom_dash" type="QString" value="1"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="4" type="line" alpha="1" force_rhr="0" clip_to_extent="1">
          <layer pass="0" enabled="1" class="SimpleLine" locked="0">
            <Option type="Map">
              <Option name="customdash" type="QString" value="2;1.5"/>
              <Option name="customdash_unit" type="QString" value="MM"/>
              <Option name="line_color" type="QString" value="5,150,105,255"/>
              <Option name="line_style" type="QString" value="dash"/>
              <Option name="line_width" type="QString" value="2.2"/>
              <Option name="line_width_unit" type="QString" value="MM"/>
              <Option name="use_custom_dash" type="QString" value="1"/>
            </Option>
          </layer>
        </symbol>
      </symbols>
      <rotation/>
      <sizescale/>
    </renderer-v2>
    <labeling type="simple">
      <settings calloutType="simple">
        <text-style fieldName="label" fontFamily="Sans" fontSize="9" fontWeight="63" textColor="20,20,20,255" textOpacity="1">
          <text-buffer bufferDraw="1" bufferSize="1" bufferColor="255,255,255,235" bufferOpacity="1"/>
        </text-style>
        <text-format/>
        <placement placement="2" priority="5" repeatDistance="0"/>
        <rendering scaleVisibility="0"/>
      </settings>
    </labeling>"""

def renderer_stops():
    return """<renderer-v2 type="RuleRenderer" forceraster="0" symbollevels="0">
      <rules key="root">
        <rule key="t" filter="&quot;terminus&quot; = 'true'" label="Terminus" symbol="0"/>
        <rule key="h" filter="&quot;highlight&quot; = 'true' AND (&quot;terminus&quot; IS NULL OR &quot;terminus&quot; != 'true')" label="Top demand" symbol="1"/>
        <rule key="p1" filter="&quot;phase&quot; = 1 AND (&quot;terminus&quot; IS NULL OR &quot;terminus&quot; != 'true') AND (&quot;highlight&quot; IS NULL OR &quot;highlight&quot; != 'true')" label="Phase 1 stop" symbol="2"/>
        <rule key="p2" filter="&quot;phase&quot; = 2 AND (&quot;terminus&quot; IS NULL OR &quot;terminus&quot; != 'true')" label="Phase 2 stop" symbol="3"/>
        <rule key="p3" filter="&quot;phase&quot; = 3 AND (&quot;terminus&quot; IS NULL OR &quot;terminus&quot; != 'true')" label="Phase 3 stop" symbol="4"/>
      </rules>
      <symbols>
        <symbol name="0" type="marker" alpha="1">
          <layer enabled="1" class="SimpleMarker" locked="0">
            <Option type="Map">
              <Option name="name" type="QString" value="circle"/>
              <Option name="color" type="QString" value="245,158,11,255"/>
              <Option name="outline_color" type="QString" value="230,57,70,255"/>
              <Option name="outline_width" type="QString" value="0.7"/>
              <Option name="size" type="QString" value="4.5"/>
              <Option name="size_unit" type="QString" value="MM"/>
              <Option name="vertical_anchor_point" type="QString" value="1"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="1" type="marker" alpha="1">
          <layer enabled="1" class="SimpleMarker" locked="0">
            <Option type="Map">
              <Option name="name" type="QString" value="circle"/>
              <Option name="color" type="QString" value="230,57,70,255"/>
              <Option name="outline_color" type="QString" value="255,255,255,255"/>
              <Option name="outline_width" type="QString" value="0.8"/>
              <Option name="size" type="QString" value="4"/>
              <Option name="size_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="2" type="marker" alpha="1">
          <layer enabled="1" class="SimpleMarker" locked="0">
            <Option type="Map">
              <Option name="name" type="QString" value="circle"/>
              <Option name="color" type="QString" value="255,255,255,255"/>
              <Option name="outline_color" type="QString" value="230,57,70,255"/>
              <Option name="outline_width" type="QString" value="0.7"/>
              <Option name="size" type="QString" value="3.2"/>
              <Option name="size_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="3" type="marker" alpha="1">
          <layer enabled="1" class="SimpleMarker" locked="0">
            <Option type="Map">
              <Option name="name" type="QString" value="circle"/>
              <Option name="color" type="QString" value="255,255,255,255"/>
              <Option name="outline_color" type="QString" value="244,162,97,255"/>
              <Option name="outline_width" type="QString" value="0.7"/>
              <Option name="size" type="QString" value="3.2"/>
              <Option name="size_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="4" type="marker" alpha="1">
          <layer enabled="1" class="SimpleMarker" locked="0">
            <Option type="Map">
              <Option name="name" type="QString" value="circle"/>
              <Option name="color" type="QString" value="255,255,255,255"/>
              <Option name="outline_color" type="QString" value="124,58,237,255"/>
              <Option name="outline_width" type="QString" value="0.7"/>
              <Option name="size" type="QString" value="2.8"/>
              <Option name="size_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
      </symbols>
    </renderer-v2>
    <labeling type="simple">
      <settings calloutType="simple">
        <text-style fieldName="name" fontFamily="Sans" fontSize="9" fontWeight="63" textColor="20,20,20,255" textOpacity="1">
          <text-buffer bufferDraw="1" bufferSize="1.2" bufferColor="255,255,255,235" bufferOpacity="1"/>
        </text-style>
        <text-format/>
        <placement placement="1" yOffset="-3" yOffsetUnit="MM" priority="8" offsetType="1"/>
        <rendering scaleVisibility="0"/>
      </settings>
    </labeling>"""

def renderer_catchments():
    return """<renderer-v2 type="categorizedSymbol" attr="phase" symbollevels="0">
      <categories>
        <category render="true" symbol="0" value="1" label="Phase 1 (400 m walk)"/>
        <category render="true" symbol="1" value="2" label="Phase 2 (400 m walk)"/>
        <category render="true" symbol="2" value="3" label="Phase 3 (400 m walk)"/>
      </categories>
      <symbols>
        <symbol name="0" type="fill" alpha="1">
          <layer enabled="1" class="SimpleFill" locked="0">
            <Option type="Map">
              <Option name="color" type="QString" value="230,57,70,40"/>
              <Option name="outline_color" type="QString" value="230,57,70,180"/>
              <Option name="outline_style" type="QString" value="solid"/>
              <Option name="outline_width" type="QString" value="0.15"/>
              <Option name="outline_width_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="1" type="fill" alpha="1">
          <layer enabled="1" class="SimpleFill" locked="0">
            <Option type="Map">
              <Option name="color" type="QString" value="244,162,97,40"/>
              <Option name="outline_color" type="QString" value="244,162,97,180"/>
              <Option name="outline_width" type="QString" value="0.15"/>
              <Option name="outline_width_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
        <symbol name="2" type="fill" alpha="1">
          <layer enabled="1" class="SimpleFill" locked="0">
            <Option type="Map">
              <Option name="color" type="QString" value="29,78,216,40"/>
              <Option name="outline_color" type="QString" value="29,78,216,180"/>
              <Option name="outline_width" type="QString" value="0.15"/>
              <Option name="outline_width_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
      </symbols>
    </renderer-v2>"""

def renderer_service_area():
    return """<renderer-v2 type="singleSymbol">
      <symbols>
        <symbol name="0" type="fill" alpha="1">
          <layer enabled="1" class="SimpleFill" locked="0">
            <Option type="Map">
              <Option name="color" type="QString" value="230,57,70,55"/>
              <Option name="outline_color" type="QString" value="230,57,70,200"/>
              <Option name="outline_style" type="QString" value="dash"/>
              <Option name="outline_width" type="QString" value="0.3"/>
              <Option name="outline_width_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
      </symbols>
    </renderer-v2>"""

def renderer_corridor():
    return """<renderer-v2 type="singleSymbol">
      <symbols>
        <symbol name="0" type="fill" alpha="1">
          <layer enabled="1" class="SimpleFill" locked="0">
            <Option type="Map">
              <Option name="color" type="QString" value="230,57,70,30"/>
              <Option name="outline_color" type="QString" value="230,57,70,140"/>
              <Option name="outline_width" type="QString" value="0.2"/>
              <Option name="outline_width_unit" type="QString" value="MM"/>
            </Option>
          </layer>
        </symbol>
      </symbols>
    </renderer-v2>"""

RENDERERS = {
    "routes": renderer_routes(),
    "stops": renderer_stops(),
    "catchments_400m": renderer_catchments(),
    "service_area_p1": renderer_service_area(),
    "corridor_150m": renderer_corridor(),
}

def vector_layer_xml(key, gpkg_layer, name, geom_type):
    layer_id = lid(key)
    return f"""    <maplayer type="vector" minScale="1e+08" maxScale="0" hasScaleBasedVisibilityFlag="0"
              autoRefreshTime="0" autoRefreshEnabled="false" wkbType="{geom_type}">
      <extent></extent>
      <id>{layer_id}</id>
      <datasource>{GPKG}|layername={gpkg_layer}</datasource>
      <layername>{name}</layername>
      <srs>
        <spatialrefsys>
          <wkt>GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],CS[ellipsoidal,2],AXIS["geodetic latitude (Lat)",north,ORDER[1],ANGLEUNIT["degree",0.0174532925199433]],AXIS["geodetic longitude (Lon)",east,ORDER[2],ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4326]]</wkt>
          <proj4>+proj=longlat +datum=WGS84 +no_defs</proj4>
          <srsid>3452</srsid>
          <srid>4326</srid>
          <authid>EPSG:4326</authid>
          <description>WGS 84</description>
          <projectionacronym>longlat</projectionacronym>
          <ellipsoidacronym>EPSG:7030</ellipsoidacronym>
          <geographicflag>true</geographicflag>
        </spatialrefsys>
      </srs>
      <provider encoding="UTF-8">ogr</provider>
      {RENDERERS[key]}
      <blendMode>0</blendMode>
      <featureBlendMode>0</featureBlendMode>
      <layerOpacity>1</layerOpacity>
      <SingleCategoryDiagramRenderer/>
      <DiagramLayerSettings/>
      <geometryOptions removeDuplicateNodes="0" geometryPrecision="0"/>
      <legend type="default-vector"/>
      <referencedLayers/>
      <fieldConfiguration/>
      <aliases/>
      <expressionfields/>
      <previewExpression>"name"</previewExpression>
    </maplayer>"""

def wms_layer_xml(layer_id, name, layers_param):
    return f"""    <maplayer type="raster" minScale="1e+08" maxScale="0" hasScaleBasedVisibilityFlag="0">
      <id>{layer_id}</id>
      <datasource>contextualWMSLegend=0&amp;crs=EPSG:3301&amp;dpiMode=7&amp;featureCount=10&amp;format=image/png&amp;layers={layers_param}&amp;styles=&amp;tileMatrixSet=&amp;url=https://kaart.maaamet.ee/wms/alus%3F</datasource>
      <layername>{name}</layername>
      <srs>
        <spatialrefsys>
          <authid>EPSG:3301</authid>
          <srid>3301</srid>
          <description>Estonian Coordinate System of 1997</description>
        </spatialrefsys>
      </srs>
      <provider>wms</provider>
      <noData><noDataList useSrcNoData="0" bandNo="1"/></noData>
      <pipe-data-defined-properties><Option type="Map"><Option name="name" type="QString" value=""/></Option></pipe-data-defined-properties>
      <pipe>
        <provider>
          <resampling enabled="false" zoomedOutResamplingMethod="nearestNeighbour" zoomedInResamplingMethod="nearestNeighbour" maxOversampling="2"/>
        </provider>
        <rasterrenderer alphaBand="-1" opacity="1" type="multibandcolor" greenBand="2" redBand="1" blueBand="3" nodataColor=""/>
        <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
        <huesaturation grayscaleMode="0" saturation="0" colorizeRed="255" colorizeOn="0" colorizeGreen="128" colorizeBlue="128" colorizeStrength="100"/>
        <rasterresampler/>
        <resamplingStage>resamplingFilter</resamplingStage>
      </pipe>
      <blendMode>0</blendMode>
    </maplayer>"""

def layer_tree_node(layer_id, name, checked="Qt::Checked"):
    return f'      <layer-tree-layer expanded="1" name="{name}" id="{layer_id}" providerKey="ogr" checked="{checked}"/>'

def build_qgs():
    layer_xmls = []
    tree_xmls = []
    custom_order = []

    # Add WMS basemaps (will be at bottom)
    wms_orto_id = "wms_orto_maaaamet_1"
    wms_topo_id = "wms_topo_maaaamet_1"
    layer_xmls.append(wms_layer_xml(wms_orto_id, "Maa-amet Orthophoto", "of10000"))
    layer_xmls.append(wms_layer_xml(wms_topo_id, "Maa-amet Topographic", "pohi,reljeef,teed"))

    # Add vector layers (will be at top)
    for key, gpkg_name, display, geom in LAYERS:
        layer_xmls.append(vector_layer_xml(key, gpkg_name, display, geom))

    # Layer tree (top-to-bottom order in QGIS Layers panel)
    # Topmost layers should be drawn last (smaller features)
    tree_xmls.append(layer_tree_node(lid("stops"),           "Tram Stops"))
    tree_xmls.append(layer_tree_node(lid("routes"),          "Tram Routes"))
    tree_xmls.append(layer_tree_node(lid("corridor_150m"),   "150 m Route Corridor", "Qt::Unchecked"))
    tree_xmls.append(layer_tree_node(lid("catchments_400m"), "Stop Catchments 400 m", "Qt::Unchecked"))
    tree_xmls.append(layer_tree_node(lid("service_area_p1"), "Phase 1 Service Area", "Qt::Unchecked"))
    tree_xmls.append(f'      <layer-tree-layer expanded="1" name="Maa-amet Topographic" id="{wms_topo_id}" providerKey="wms" checked="Qt::Unchecked"/>')
    tree_xmls.append(f'      <layer-tree-layer expanded="1" name="Maa-amet Orthophoto" id="{wms_orto_id}" providerKey="wms" checked="Qt::Checked"/>')

    custom_order = [
        lid("stops"), lid("routes"), lid("corridor_150m"),
        lid("catchments_400m"), lid("service_area_p1"),
        wms_topo_id, wms_orto_id,
    ]

    qgs = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0-Prizren" projectname="Tartu Tramline GIS" saveDateTime="2026-05-18T12:00:00">
  <homePath path=""/>
  <title>Tartu Tramline — General Plan GIS</title>
  <autotransaction active="0"/>
  <evaluateDefaultValues active="0"/>
  <trust active="0"/>
  <projectCrs>
    <spatialrefsys>
      <wkt>PROJCRS["Estonian Coordinate System of 1997",BASEGEOGCRS["EST97",DATUM["Estonia 1997",ELLIPSOID["GRS 1980",6378137,298.257222101,LENGTHUNIT["metre",1]]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]]],CONVERSION["Lambert Conic Conformal (2SP)",METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],PARAMETER["Latitude of false origin",57.5174,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8821]],PARAMETER["Longitude of false origin",24,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8822]],PARAMETER["Latitude of 1st standard parallel",59.3333333333333,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8823]],PARAMETER["Latitude of 2nd standard parallel",58,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8824]],PARAMETER["Easting at false origin",500000,LENGTHUNIT["metre",1],ID["EPSG",8826]],PARAMETER["Northing at false origin",6375000,LENGTHUNIT["metre",1],ID["EPSG",8827]]],CS[Cartesian,2],AXIS["northing (X)",north,ORDER[1],LENGTHUNIT["metre",1]],AXIS["easting (Y)",east,ORDER[2],LENGTHUNIT["metre",1]],ID["EPSG",3301]]</wkt>
      <proj4>+proj=lcc +lat_0=57.5175539305556 +lon_0=24 +lat_1=59.3333333333333 +lat_2=58 +x_0=500000 +y_0=6375000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs</proj4>
      <srsid>2103</srsid>
      <srid>3301</srid>
      <authid>EPSG:3301</authid>
      <description>Estonian Coordinate System of 1997</description>
      <projectionacronym>lcc</projectionacronym>
      <ellipsoidacronym>EPSG:7019</ellipsoidacronym>
      <geographicflag>false</geographicflag>
    </spatialrefsys>
  </projectCrs>

  <layer-tree-group>
    <customproperties/>
{chr(10).join(tree_xmls)}
    <layer-tree-group expanded="1" name="" checked="Qt::Checked">
      <customproperties/>
    </layer-tree-group>
    <custom-order enabled="0">
{chr(10).join(f"      <item>{l}</item>" for l in custom_order)}
    </custom-order>
  </layer-tree-group>

  <snapping-settings tolerance="12" mode="2" unit="1" enabled="0" type="1"/>
  <relations/>
  <polymorphicRelations/>

  <mapcanvas name="theMapCanvas" annotationsVisible="1">
    <units>meters</units>
    <extent>
      <xmin>650000</xmin><ymin>6469500</ymin>
      <xmax>662000</xmax><ymax>6476000</ymax>
    </extent>
    <rotation>0</rotation>
    <destinationsrs>
      <spatialrefsys>
        <authid>EPSG:3301</authid>
        <srid>3301</srid>
        <description>Estonian Coordinate System of 1997</description>
      </spatialrefsys>
    </destinationsrs>
    <rendermaptile>0</rendermaptile>
    <expressionContextScope/>
  </mapcanvas>

  <projectModels/>
  <legend updateDrawingOrder="true"/>

  <mapViewDocks/>
  <mapViewDocks3D/>

  <projectlayers>
{chr(10).join(layer_xmls)}
  </projectlayers>

  <layerorder>
{chr(10).join(f"    <layer id='{l}'/>" for l in custom_order)}
  </layerorder>

  <properties>
    <Gui>
      <CanvasColor type="QString">#f7f8fa</CanvasColor>
      <SelectionColor type="QString">#ffff00</SelectionColor>
    </Gui>
    <Measurement>
      <DistanceUnits type="QString">meters</DistanceUnits>
      <AreaUnits type="QString">m2</AreaUnits>
    </Measurement>
    <PAL>
      <SearchMethod type="int">0</SearchMethod>
      <CandidatesLinePerCM type="double">5</CandidatesLinePerCM>
      <CandidatesPolygonPerCM type="double">2.5</CandidatesPolygonPerCM>
      <ShowingShadowRects type="bool">false</ShowingShadowRects>
    </PAL>
  </properties>

  <visibility-presets/>
  <transformContext/>
  <projectMetadata>
    <identifier>tartu-tramline-2040</identifier>
    <parentidentifier>open-gis</parentidentifier>
    <language>en-EE</language>
    <type>dataset</type>
    <title>Tartu Tramline General Plan — GIS Analysis</title>
    <abstract>Geospatial analysis of the proposed Tartu tramline based on the 2020 feasibility study (Civitta/Artes Terrae/Stratum) and Üldplaneering 2040+. Includes 4 lines, 21 stops, catchment buffers, service area, and corridor analysis. All coordinates verified against actual addresses.</abstract>
    <keywords vocabulary="gmd:topicCategory">
      <keyword>transportation</keyword>
      <keyword>planning</keyword>
      <keyword>tram</keyword>
      <keyword>Tartu</keyword>
      <keyword>Estonia</keyword>
    </keywords>
    <creation>2026-05-18T12:00:00</creation>
  </projectMetadata>
  <Annotations/>
  <Layouts/>
  <Bookmarks/>
  <Sensors/>
</qgis>
"""

    out = ROOT / "tartu-tramline.qgs"
    out.write_text(qgs, encoding="utf-8")
    print(f"✓ Wrote {out} ({len(qgs):,} bytes)")
    print(f"  Open in QGIS 3.x via: File → Open → {out.name}")
    print(f"  Includes 5 vector layers + Maa-amet WMS basemaps (orthophoto/topo)")

if __name__ == "__main__":
    build_qgs()
