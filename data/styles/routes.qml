<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0-Prizren" styleCategories="Symbology|Labeling">
  <renderer-v2 type="categorizedSymbol" attr="id" symbollevels="0">
    <categories>
      <category render="true" symbol="0" value="L1P1" label="Line 1 — Phase 1 (Core)"/>
      <category render="true" symbol="1" value="L1P2" label="Line 1 — Phase 2 (Ränilinn ext.)"/>
      <category render="true" symbol="2" value="L2" label="Line 2 — Ropka (Phase 3)"/>
      <category render="true" symbol="3" value="L3" label="Line 3 — Raadi (Phase 3)"/>
      <category render="true" symbol="4" value="HOLMI_BRIDGE" label="New Holmi Bridge"/>
    </categories>
    <symbols>
      <symbol name="0" type="line" alpha="1">
        <layer class="SimpleLine">
          <Option type="Map">
            <Option name="line_color" type="QString" value="230,57,70,255"/>
            <Option name="line_width" type="QString" value="1.6"/>
            <Option name="line_width_unit" type="QString" value="MM"/>
            <Option name="capstyle" type="QString" value="round"/>
            <Option name="joinstyle" type="QString" value="round"/>
          </Option>
        </layer>
      </symbol>
      <symbol name="1" type="line" alpha="1">
        <layer class="SimpleLine">
          <Option type="Map">
            <Option name="line_color" type="QString" value="244,162,97,255"/>
            <Option name="line_width" type="QString" value="1.4"/>
            <Option name="line_width_unit" type="QString" value="MM"/>
            <Option name="customdash" type="QString" value="3;1.2"/>
            <Option name="use_custom_dash" type="QString" value="1"/>
            <Option name="capstyle" type="QString" value="round"/>
          </Option>
        </layer>
      </symbol>
      <symbol name="2" type="line" alpha="1">
        <layer class="SimpleLine">
          <Option type="Map">
            <Option name="line_color" type="QString" value="29,78,216,255"/>
            <Option name="line_width" type="QString" value="1.2"/>
            <Option name="line_width_unit" type="QString" value="MM"/>
            <Option name="customdash" type="QString" value="2.5;1.5"/>
            <Option name="use_custom_dash" type="QString" value="1"/>
          </Option>
        </layer>
      </symbol>
      <symbol name="3" type="line" alpha="1">
        <layer class="SimpleLine">
          <Option type="Map">
            <Option name="line_color" type="QString" value="124,58,237,255"/>
            <Option name="line_width" type="QString" value="1.2"/>
            <Option name="line_width_unit" type="QString" value="MM"/>
            <Option name="customdash" type="QString" value="2.5;1.5"/>
            <Option name="use_custom_dash" type="QString" value="1"/>
          </Option>
        </layer>
      </symbol>
      <symbol name="4" type="line" alpha="1">
        <layer class="SimpleLine">
          <Option type="Map">
            <Option name="line_color" type="QString" value="5,150,105,255"/>
            <Option name="line_width" type="QString" value="2.2"/>
            <Option name="line_width_unit" type="QString" value="MM"/>
            <Option name="customdash" type="QString" value="2;1.5"/>
            <Option name="use_custom_dash" type="QString" value="1"/>
          </Option>
        </layer>
      </symbol>
    </symbols>
  </renderer-v2>
  <labeling type="simple">
    <settings>
      <text-style fieldName="label" fontFamily="Sans" fontSize="9" fontWeight="50" textColor="40,40,40,255"
                  fontUnderline="0" fontItalic="0" fontStrikeout="0" textOpacity="1">
        <text-buffer bufferDraw="1" bufferSize="1" bufferColor="255,255,255,220" bufferOpacity="1"/>
      </text-style>
      <text-format/>
      <placement placement="2" priority="5"/>
    </settings>
  </labeling>
</qgis>
