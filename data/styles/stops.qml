<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34.0-Prizren" styleCategories="Symbology|Labeling">
  <renderer-v2 type="RuleRenderer">
    <rules key="root">
      <rule key="t" filter="&quot;terminus&quot; = 'true' OR &quot;terminus&quot; = true" label="Terminus" symbol="0"/>
      <rule key="h" filter="&quot;highlight&quot; = 'true' OR &quot;highlight&quot; = true" label="Top demand" symbol="1"/>
      <rule key="p1" filter="&quot;phase&quot; = 1" label="Phase 1 stop" symbol="2"/>
      <rule key="p2" filter="&quot;phase&quot; = 2" label="Phase 2 stop" symbol="3"/>
      <rule key="p3" filter="&quot;phase&quot; = 3" label="Phase 3 stop" symbol="4"/>
    </rules>
    <symbols>
      <symbol name="0" type="marker" alpha="1">
        <layer class="SimpleMarker">
          <Option type="Map">
            <Option name="name" type="QString" value="circle"/>
            <Option name="color" type="QString" value="245,158,11,255"/>
            <Option name="outline_color" type="QString" value="230,57,70,255"/>
            <Option name="outline_width" type="QString" value="0.7"/>
            <Option name="size" type="QString" value="4.5"/>
            <Option name="size_unit" type="QString" value="MM"/>
          </Option>
        </layer>
      </symbol>
      <symbol name="1" type="marker" alpha="1">
        <layer class="SimpleMarker">
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
        <layer class="SimpleMarker">
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
        <layer class="SimpleMarker">
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
        <layer class="SimpleMarker">
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
    <settings>
      <text-style fieldName="name" fontFamily="Sans" fontSize="9" fontWeight="63" textColor="20,20,20,255" textOpacity="1">
        <text-buffer bufferDraw="1" bufferSize="1.2" bufferColor="255,255,255,235" bufferOpacity="1"/>
      </text-style>
      <placement placement="1" offsetType="1" labelOffsetMapUnitScale="0,0" xOffset="0" yOffset="-3"
                 yOffsetUnit="MM" xOffsetUnit="MM" priority="8"/>
    </settings>
  </labeling>
</qgis>
