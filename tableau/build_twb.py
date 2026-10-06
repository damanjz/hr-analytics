"""Generate the HR analytics workbench as a packaged Tableau workbook (.twbx).

How it works, and why this format:
- The workbook is written in Tableau's current XML (version 18.1), modelled element by element on a
  file Tableau Public 2026.2.2 saved itself. Older-format files load through Tableau's upgrade path,
  but that path throws away custom colour palettes.
- Data travels inside the package as a Hyper extract (Tableau Public only accepts extracts), written
  by make_extract.py from the DuckDB table hr_workbench: one long table with a row type per subject
  (Month, Employee, Requisition, Survey), so every filter reaches every view.
- Filters are parameters (department, location, level band, fiscal year) combined in one
  calculated field, In scope, that every sheet filters on.
"""
import argparse
import re
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import duckdb

import make_extract

ROOT = Path(__file__).resolve().parents[1]
NAME = "HR Analytics Workbench"
OUT = ROOT / "tableau" / f"{NAME}.twbx"
DS = "federated.hrworkbench"
CONN = "textscan.hrworkbench"
VERSION = "18.1"
HEADER = f"source-build='2026.2.2 (20262.26.0819.2015)' source-platform='win' version='{VERSION}'"
# Format features Tableau 2026.2.2 declares when it saves this workbook
MANIFEST = ("AnimationOnByDefault", "MarkAnimation", "ObjectModelEncapsulateLegacy", "ObjectModelExtractV2",
            "ObjectModelTableType", "SchemaViewerObjectModel", "SetMembershipControl", "SheetIdentifierTracking",
            "SortTagCleanup", "VConnDownstreamExtractsWithWarnings", "WindowsPersistSimpleIdentifiers")
TABLE = "hr_workbench"   # the one logical table (object) in the data source


def simple_id(kind, name):
    """Stable uuid per sheet, dashboard or window, so two builds are byte-identical."""
    return "<simple-id uuid='{%s}' />" % str(uuid.uuid5(uuid.NAMESPACE_URL, f"hr-analytics/{kind}/{name}")).upper()

INK, OCHRE, TEAL, RUST, PLUM = "#2f55b0", "#c27a1a", "#1f8f7a", "#c0533a", "#8456b0"
PAPER, RAIL, RULE, TEXT, MUTED = "#fbfaf7", "#f2efe7", "#e3ded2", "#1d2330", "#5b6170"
SERIF, SANS = "Georgia", "Segoe UI"

TYPE_MAP = {"VARCHAR": "string", "DATE": "date", "DOUBLE": "real", "BIGINT": "integer", "INTEGER": "integer",
            "BOOLEAN": "boolean", "HUGEINT": "integer"}


def q(v):
    return quoteattr(str(v))


def f(name):
    """Fully qualified field reference."""
    return f"[{DS}].[{name}]"


# ---------------------------------------------------------------- parameters
def param_values():
    con = duckdb.connect(str(ROOT / "data" / "hr.duckdb"), read_only=True)
    vals = {c: [r[0] for r in con.sql(f"SELECT DISTINCT {c} FROM hr_workbench WHERE {c} IS NOT NULL ORDER BY 1").fetchall()]
            for c in ("department", "location", "level_band", "fiscal_year")}
    con.close()
    return vals


PARAMS = [("Parameter 1", "Department", "department"), ("Parameter 2", "Location", "location"),
          ("Parameter 3", "Level", "level_band"), ("Parameter 4", "Year", "fiscal_year")]


PARAM_VALUES = {}   # field -> list of values, read at build time
PRESET = {}         # parameter id -> starting value (test switch; default "All")


def param_columns(indent, members=True):
    """Parameter column declarations; Tableau repeats them in every sheet and dashboard that uses them."""
    pad, cols = " " * indent, []
    for pid, caption, field in PARAMS:
        start = escape(chr(34) + PRESET.get(pid, "All") + chr(34), {'"': "&quot;"})
        body = f"{pad}  <calculation class='tableau' formula='{start}' />\n"
        if members:
            body += f"{pad}  <members>\n" + "".join(f"{pad}    <member value={q(chr(34) + v + chr(34))} />\n"
                                                for v in ["All"] + PARAM_VALUES[field]) + f"{pad}  </members>\n"
        cols.append(f"{pad}<column caption={q(caption)} datatype='string' name='[{pid}]' param-domain-type='list' "
                    f"role='measure' type='nominal' value='{start}'>\n{body}{pad}</column>")
    return "\n".join(cols)


def param_dependencies(indent, members=False):
    pad = " " * indent
    return f"{pad}<datasource-dependencies datasource='Parameters'>\n{param_columns(indent + 2, members)}\n{pad}</datasource-dependencies>"


def parameters_datasource():
    return f"""    <datasource hasconnection='false' inline='true' name='Parameters' version='{VERSION}'>
      <aliases enabled='yes' />
{param_columns(6)}
    </datasource>"""


# ---------------------------------------------------------------- calculated fields
# name -> (caption, datatype, role, type, formula)
MIN_N = 5   # bars resting on fewer records than this are left empty (too few to read as a rate)
CALCS = {
    "Calc_InScope": ("In scope", "boolean", "dimension", "nominal",
                     "([Parameters].[Parameter 1] = 'All' OR [department] = [Parameters].[Parameter 1]) "
                     "AND ([Parameters].[Parameter 2] = 'All' OR [location] = [Parameters].[Parameter 2]) "
                     "AND ([Parameters].[Parameter 3] = 'All' OR [level_band] = [Parameters].[Parameter 3]) "
                     "AND ([Parameters].[Parameter 4] = 'All' OR [fiscal_year] = [Parameters].[Parameter 4] "
                     "OR [record_type] = 'Employee' OR [record_type] = 'Driver')"),
    "Calc_AttritionRate": ("Attrition rate (annualised)", "real", "measure", "quantitative",
                           "SUM([exits_voluntary] + [exits_involuntary]) / SUM([headcount]) * 12"),
    "Calc_VoluntaryRate": ("Voluntary attrition (annualised)", "real", "measure", "quantitative",
                           "SUM([exits_voluntary]) / SUM([headcount]) * 12"),
    "Calc_Attrition12": ("Attrition, rolling 12 months", "real", "measure", "quantitative",
                         "SUM([exits_12m]) / SUM([headcount_12m]) * 12"),
    "Calc_VoluntaryShare": ("Voluntary share of exits", "real", "measure", "quantitative",
                            "SUM([exits_voluntary]) / SUM([exits_voluntary] + [exits_involuntary])"),
    "Calc_WomenShare": ("Women share", "real", "measure", "quantitative",
                        f"IIF(SUM(IIF([record_type] = 'Employee', 1, 0)) >= {MIN_N}, "
                        "SUM(IIF([record_type] = 'Employee' AND [gender] = 'Woman', 1, 0)) "
                        "/ SUM(IIF([record_type] = 'Employee', 1, 0)), NULL)"),
    "Calc_Employees": ("Employees", "integer", "measure", "quantitative",
                       "SUM(IIF([record_type] = 'Employee', 1, 0))"),
    "Calc_TimeToHire": ("Median time to hire (days)", "real", "measure", "quantitative",
                        "MEDIAN([time_to_hire_days])"),
    "Calc_TimeToHireByLevel": ("Median time to hire (days, 5+ hires)", "real", "measure", "quantitative",
                               f"IIF(COUNT([time_to_hire_days]) >= {MIN_N}, MEDIAN([time_to_hire_days]), NULL)"),
    "Calc_eNPS": ("eNPS", "real", "measure", "quantitative",
                  "(SUM(IIF([enps_category] = 'Promoter', 1, 0)) - SUM(IIF([enps_category] = 'Detractor', 1, 0))) "
                  "* 100 / SUM(IIF([record_type] = 'Survey', 1, 0))"),
    "Calc_PayRatio": ("Pay ratio, women to men (median)", "real", "measure", "quantitative",
                      "MEDIAN(IIF([gender] = 'Woman', [annual_ctc_lakh], NULL)) / MEDIAN(IIF([gender] = 'Man', [annual_ctc_lakh], NULL))"),
    "Calc_RiskBin": ("Risk score band", "real", "dimension", "quantitative",
                     "FLOOR([risk_score] * 50) / 50"),
    "Calc_RatingJitter": ("Rating (spread)", "real", "measure", "quantitative",
                          "[rating] + (INT(RIGHT([emp_id], 3)) % 41 - 20) / 60"),
    "Calc_Rated": ("Has a rating", "boolean", "dimension", "nominal", "NOT ISNULL([rating])"),
    "Calc_LeverPeople": ("People", "integer", "measure", "quantitative", "SUM(IIF([record_type] = 'Driver', 1, 0))"),
}


FORMATS = {"Calc_AttritionRate": "p0.0%", "Calc_Attrition12": "p0%", "Calc_VoluntaryRate": "p0.0%", "Calc_VoluntaryShare": "p0%",
           "Calc_WomenShare": "p0%", "Calc_TimeToHire": "n#,##0", "Calc_TimeToHireByLevel": "n#,##0", "Calc_eNPS": "n#,##0;-#,##0",
           "Calc_PayRatio": "p0.0%", "Calc_RiskBin": "p0%", "risk_percentile": "p0.0%", "risk_score": "p0.0%",
           "compa_ratio": "n0.00"}

# Colour assignments live on the data source, where Tableau keeps them.
PALETTES = {}   # filled below once the colour constants exist


def fmt_attr(name):
    return f" default-format={q(FORMATS[name])}" if name in FORMATS else ""


def referenced(formula):
    """Base columns a formula mentions (parameters, written [Parameters].[...], are excluded)."""
    return set(re.findall(r"(?<![.\]])\[([a-z_]+)\]", formula))


def calc_columns():
    out = []
    for name, (cap, dt, role, typ, formula) in CALCS.items():
        out.append(f"""      <column caption={q(cap)} datatype={q(dt)}{fmt_attr(name)} name='[{name}]' role={q(role)} type={q(typ)}>
        <calculation class='tableau' formula={q(formula)} />
      </column>""")
    return "\n".join(out)


# ---------------------------------------------------------------- data source
REMOTE_TYPE = {'string': 129, 'date': 133, 'real': 5, 'integer': 20, 'boolean': 11}


def metadata_records(cols, parent, extract):
    """Column metadata for the CSV connection (parent = the CSV relation) or the extract."""
    out = []
    for i, (c, t) in enumerate(cols):
        if not extract:
            tail = f"              <object-id>[{TABLE}]</object-id>\n"
        else:
            tail = "              <collation flag='0' name='binary' />\n" if t == "string" else ""
        agg = "Sum" if t in ("real", "integer") else "Year" if t == "date" else "Count"
        out.append(f"""            <metadata-record class='column'>
              <remote-name>{escape(c)}</remote-name>
              <remote-type>{REMOTE_TYPE[t]}</remote-type>
              <local-name>[{escape(c)}]</local-name>
              <parent-name>{parent}</parent-name>
              <remote-alias>{escape(c)}</remote-alias>
              <ordinal>{i}</ordinal>
              <local-type>{t}</local-type>
              <aggregation>{agg}</aggregation>
              <contains-null>true</contains-null>
{tail}            </metadata-record>""")
    return "\n".join(out)


def datasource(cols):
    rel_cols = "\n".join(f"            <column datatype={q(t)} name={q(c)} ordinal={q(i)} />" for i, (c, t) in enumerate(cols))
    csv_relation = f"""<relation connection='{CONN}' name='hr_workbench.csv' table='[hr_workbench#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_US' separator=','>
{rel_cols}
          </columns>
        </relation>"""
    base_cols = "\n".join(
        f"      <column{cap_attr(c)} datatype={q(t)}{fmt_attr(c)} name={q('[' + c + ']')} role={q(COLTYPES[c][1])} type={q(COLTYPES[c][2])} />"
        for c, t in cols)
    palette_insts = "\n".join(instance(fld, "None")[1].strip().join(["      ", ""]) for fld in PALETTES)
    graph_relation = csv_relation.replace("\n          ", "\n                  ").replace("\n        </relation>", "\n                </relation>")
    return f"""    <datasource caption='HR workbench' inline='true' name='{DS}' version='{VERSION}'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='hr_workbench' name='{CONN}'>
            <connection class='textscan' directory='Data/hr_workbench' filename='hr_workbench.csv' workgroup-auth-mode='as-is' />
          </named-connection>
        </named-connections>
        {csv_relation}
        <metadata-records>
{metadata_records(cols, '[hr_workbench.csv]', False)}
        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{base_cols}
{calc_columns()}
      <column caption='{TABLE}' datatype='table' name='[__tableau_internal_object_id__].[{TABLE}]' role='measure' type='quantitative' />
{palette_insts}
      <extract count='-1' enabled='true' object-id='{TABLE}' units='records' user-specific='false'>
        <connection author-locale='en_US' class='hyper' dbname='Data/Extracts/hr_workbench.hyper' default-settings='yes' schema='Extract' sslmode='' tablename='Extract' update-time='10/06/2026 05:00:00 PM'>
          <relation name='Extract' table='[Extract].[Extract]' type='table' />
          <metadata-records>
{metadata_records(cols, '[Extract]', True)}
          </metadata-records>
        </connection>
      </extract>
      <layout dim-ordering='alphabetic' measure-ordering='alphabetic' show-structure='true' />
      <style>
{palette_rules()}
      </style>
{param_dependencies(6)}
      <object-graph>
        <objects>
          <object caption='{TABLE}' id='{TABLE}'>
            <properties context=''>
              {graph_relation}
            </properties>
            <properties context='extract'>
              <relation name='Extract' table='[Extract].[Extract]' type='table' />
            </properties>
          </object>
        </objects>
      </object-graph>
    </datasource>"""


def palette_rules():
    # One mark rule holding every encoding, as Tableau writes it: a second mark rule replaces the first.
    out = []
    for fld, mapping in PALETTES.items():
        maps = "\n".join(f"            <map to='{hex_}'>\n              <bucket>&quot;{k}&quot;</bucket>\n            </map>"
                         for k, hex_ in mapping.items())
        out.append(f"""          <encoding attr='color' field='[none:{fld}:nk]' type='palette'>
{maps}
          </encoding>""")
    return "        <style-rule element='mark'>\n" + "\n".join(out) + "\n        </style-rule>" if out else ""


# ---------------------------------------------------------------- worksheets
COLTYPES = {}   # field -> (datatype, role, type), filled from the CSV schema and CALCS
FILTER_MODE = "all"   # all | type | none  (test switch: which sheet filters to emit)


# Display names for raw columns (shown as table headers and default axis titles)
CAPTIONS = {"emp_id": "Employee", "department": "Department", "level": "Level", "drivers": "What drives the score",
            "compa_ratio": "Pay vs band midpoint", "risk_score": "Risk score"}


def cap_attr(field):
    return f" caption={q(CAPTIONS[field])}" if field in CAPTIONS else ""


def dep_column(field):
    dt, role, typ = COLTYPES[field]
    if field in CALCS:
        cap, dt, role, typ, formula = CALCS[field]
        return (f"            <column caption={q(cap)} datatype={q(dt)}{fmt_attr(field)} name='[{field}]' role={q(role)} type={q(typ)}>\n"
                f"              <calculation class='tableau' formula={q(formula)} />\n            </column>")
    return f"            <column{cap_attr(field)} datatype={q(dt)}{fmt_attr(field)} name='[{field}]' role={q(role)} type={q(typ)} />"


def instance(field, deriv):
    """Column-instance name for a field with a derivation ('None', 'Sum', 'Avg', 'User', 'Month-Trunc', 'Count')."""
    prefix = {"None": "none", "Sum": "sum", "Avg": "avg", "User": "usr", "Month-Trunc": "tmn", "Count": "cnt",
              "Median": "med", "CountD": "ctd"}[deriv]
    dt, role, typ = COLTYPES[field]
    if deriv == "None" and role == "dimension":
        kind, t = {"ordinal": "ok", "quantitative": "qk"}.get(typ, "nk"), typ
    elif deriv == "Month-Trunc":
        kind, t = "qk", "quantitative"
    else:
        kind, t = "qk", "quantitative"
    name = f"[{prefix}:{field}:{kind}]"
    xml = f"            <column-instance column='[{field}]' derivation={q(deriv)} name='{name}' pivot='key' type={q(t)} />"
    return name, xml


def shelf(refs):
    """Nest fields on a shelf the way Tableau writes them: (a / (b / c))."""
    if len(refs) <= 1:
        return "".join(refs)
    return f"({refs[0]} / {shelf(refs[1:])})"


class Sheet:
    def __init__(self, name, record_type, mark, rows=(), cols=(), color=None, text=None, size=None, detail=(),
                 aggregate=True, extra_filters=(), sort=None, palette=None, label_marks=False, fmt=None, drop_null=None, axes=(), hide_labels=(), widths=()):
        self.name, self.record_type, self.mark = name, record_type, mark
        self.rows, self.cols, self.color, self.text, self.size = rows, cols, color, text, size
        self.detail, self.aggregate, self.extra_filters, self.sort = detail, aggregate, extra_filters, sort
        self.palette, self.label_marks, self.fmt = palette, label_marks, fmt
        self.drop_null = drop_null   # (field, derivation) whose NULL rows are dropped (small groups, no history)
        self.axes = axes             # (field, derivation, scope, title or None, (origin, spacing) or None)
        self.hide_labels = hide_labels   # shelves ('rows'/'cols') whose field-name header is hidden
        self.widths = widths         # (field, derivation, pixels) for table column widths
        self.extra_rules = []

    def xml(self):
        fields, insts = set(), {}

        def use(field, deriv):
            fields.add(field)
            name, x = instance(field, deriv)
            insts[name] = x
            return f"{f(name[1:-1])}"

        rows, cols = shelf([use(*r) for r in self.rows]), shelf([use(*c) for c in self.cols])
        enc = []
        if self.color:
            enc.append(f"<color column='{use(*self.color)}' />")
        if self.size:
            enc.append(f"<size column='{use(*self.size)}' />")
        if self.text:
            for t in self.text:
                enc.append(f"<text column='{use(*t)}' />")
        for d in self.detail:
            enc.append(f"<lod column='{use(*d)}' />")
        # filters: record type and scope
        rt = use("record_type", "None")
        scope = use("Calc_InScope", "None")
        filters = [f"""          <filter class='categorical' column='{rt}'>
            <groupfilter function='member' level='[none:record_type:nk]' member='&quot;{self.record_type}&quot;' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
          </filter>""",
                   f"""          <filter class='categorical' column='{scope}'>
            <groupfilter function='member' level='[none:Calc_InScope:nk]' member='true' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
          </filter>"""]
        slices = [rt, scope]
        if FILTER_MODE == "type":
            filters, slices = filters[:1], slices[:1]
        elif FILTER_MODE == "none":
            filters, slices = [], []
        for fld, member in self.extra_filters:
            ref = use(fld, "None")
            filters.append(f"""          <filter class='categorical' column='{ref}'>
            <groupfilter function='member' level='[none:{fld}:nk]' member='{member if member in ("true", "false") else "&quot;" + member + "&quot;"}' user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />
          </filter>""")
            slices.append(ref)
        axis, header = [], []
        for fld, deriv, scope, title, tick in self.axes:
            ref = use(fld, deriv)
            if tick:
                axis.append(f"<encoding attr='space' class='0' field='{ref}' field-type='quantitative' "
                            f"major-origin='{tick[0]}' major-spacing='{tick[1]}' scope='{scope}' type='space' />")
            if title is not None:
                axis.append(f"<format attr='title' class='0' field='{ref}' scope='{scope}' value={q(title)} />")
        for fld, deriv, px in self.widths:
            header.append(f"<format attr='width' field='{use(fld, deriv)}' value='{px}' />")
        self.extra_rules = [(el, items) for el, items in (("axis", axis), ("header", header)) if items]
        if self.drop_null:
            ref = use(*self.drop_null)
            filters.append(f"          <filter class='quantitative' column='{ref}' included-values='non-null' />")
            slices.append(ref)
        sort = ""
        if self.sort:
            dim, meas, direction = self.sort
            sort = f"          <computed-sort column='{use(*dim)}' direction='{direction}' using='{use(*meas)}' />\n"
        for fld in list(fields):
            if fld in CALCS:
                fields |= referenced(CALCS[fld][4]) & set(COLTYPES)
        deps = "\n".join(dep_column(x) for x in sorted(fields)) + "\n" + "\n".join(insts.values())
        encodings = ("\n            <encodings>\n              " + "\n              ".join(enc) + "\n            </encodings>") if enc else ""
        style_rules = self.style()
        return f"""    <worksheet name={q(self.name)}>
      <table>
        <view>
          <datasources>
            <datasource caption='HR workbench' name='{DS}' />
            <datasource name='Parameters' />
          </datasources>
{param_dependencies(10)}
          <datasource-dependencies datasource='{DS}'>
{deps}
          </datasource-dependencies>
{chr(10).join(filters)}
{sort}          <slices>
{chr(10).join('            <column>' + s + '</column>' for s in slices)}
          </slices>
          <aggregation value='{'true' if self.aggregate else 'false'}' />
        </view>
        <style>
{style_rules}
        </style>
        <panes>
          <pane selection-relaxation-option='selection-relaxation-disallow'>
            <view>
              <breakdown value='auto' />
            </view>
            <mark class={q(self.mark)} />{encodings}
            <style>
              <style-rule element='mark'>
                <format attr='mark-labels-show' value='{'true' if self.label_marks else 'false'}' />{self.mark_color()}
              </style-rule>
            </style>
          </pane>
        </panes>
        {f'<rows>{rows}</rows>' if rows else '<rows />'}
        {f'<cols>{cols}</cols>' if cols else '<cols />'}
      </table>
      {simple_id('worksheet', self.name)}
    </worksheet>"""

    def mark_color(self):
        return "" if self.color else f"\n                <format attr='mark-color' value='{INK}' />"

    def style(self):
        rules = [f"""          <style-rule element='worksheet'>
            <format attr='font-family' value='{SANS}' />
            <format attr='font-size' value='9' />
            <format attr='color' value='{MUTED}' />{''.join(chr(10) + f"            <format attr='display-field-labels' scope='{sc}' value='false' />" for sc in self.hide_labels)}
          </style-rule>""",
                 f"""          <style-rule element='gridline'>
            <format attr='line-visibility' scope='cols' value='off' />
            <format attr='line-visibility' scope='rows' value='off' />
          </style-rule>""",
                 f"""          <style-rule element='table'>
            <format attr='background-color' value='{PAPER}' />
          </style-rule>"""]
        if self.mark == "Text" and self.name.startswith("KPI"):
            rules.append(f"""          <style-rule element='cell'>
            <format attr='font-family' value='{SERIF}' />
            <format attr='font-size' value='24' />
            <format attr='color' value='{TEXT}' />
            <format attr='text-align' value='left' />
          </style-rule>""")
        rules.append("""          <style-rule element='table-div'>
            <format attr='line-visibility' scope='cols' value='off' />
            <format attr='line-visibility' scope='rows' value='off' />
          </style-rule>
          <style-rule element='header-div'>
            <format attr='line-visibility' scope='cols' value='off' />
            <format attr='line-visibility' scope='rows' value='off' />
          </style-rule>""")
        for el, items in self.extra_rules:
            rules.append(f"          <style-rule element='{el}'>\n" + "".join(f"            {x}\n" for x in items) + "          </style-rule>")
        return "\n".join(rules)


GENDER = {"Woman": PLUM, "Man": INK, "Non-binary": OCHRE}
BAND = {"High": RUST, "Medium": OCHRE, "Low": "#9aa6c8"}
POSITION = {"Below band": RUST, "Within band": "#9aa6c8", "Above band": TEAL}
PALETTES.update({"risk_band": BAND, "pay_position": POSITION, "gender": GENDER})


def sheets():
    return [
        Sheet("KPI attrition", "Month", "Text", text=[("Calc_AttritionRate", "User")]),
        Sheet("KPI voluntary", "Month", "Text", text=[("Calc_VoluntaryShare", "User")]),
        Sheet("KPI time to hire", "Requisition", "Text", text=[("Calc_TimeToHire", "User")]),
        Sheet("KPI eNPS", "Survey", "Text", text=[("Calc_eNPS", "User")]),
        Sheet("KPI pay ratio", "Employee", "Text", text=[("Calc_PayRatio", "User")]),
        Sheet("Attrition trend", "Month", "Line", cols=[("date", "Month-Trunc")], rows=[("Calc_Attrition12", "User")],
              drop_null=("Calc_Attrition12", "User"),
              axes=[("Calc_Attrition12", "User", "rows", "", None), ("date", "Month-Trunc", "cols", "", None)]),
        Sheet("Flight risk", "Employee", "Bar", cols=[("Calc_RiskBin", "None")], rows=[("Calc_Employees", "User")],
              color=("risk_band", "None"),
              axes=[("Calc_RiskBin", "None", "cols", "", (0, 0.1)), ("Calc_Employees", "User", "rows", "", None)]),
        Sheet("Women by level", "Employee", "Bar", rows=[("level", "None")], cols=[("Calc_WomenShare", "User")],
              label_marks=True, drop_null=("Calc_WomenShare", "User"), hide_labels=["rows"],
              axes=[("Calc_WomenShare", "User", "cols", "", None)]),
        Sheet("Pay vs rating", "Employee", "Circle", cols=[("Calc_RatingJitter", "None")], rows=[("compa_ratio", "None")],
              color=("pay_position", "None"), detail=[("emp_id", "None")], aggregate=False,
              extra_filters=[("Calc_Rated", "true")],
              axes=[("Calc_RatingJitter", "None", "cols", "Performance rating", (1, 1)),
                    ("compa_ratio", "None", "rows", "Pay vs band midpoint", None)]),
        Sheet("Time to hire", "Requisition", "Bar", rows=[("level", "None")], cols=[("Calc_TimeToHireByLevel", "User")],
              label_marks=True, drop_null=("Calc_TimeToHireByLevel", "User"), hide_labels=["rows"],
              axes=[("Calc_TimeToHireByLevel", "User", "cols", "", None)]),
        Sheet("eNPS trend", "Survey", "Line", cols=[("date", "Month-Trunc")], rows=[("Calc_eNPS", "User")],
              axes=[("Calc_eNPS", "User", "rows", "", None), ("date", "Month-Trunc", "cols", "", None)]),
        Sheet("Risk levers", "Driver", "Bar", rows=[("drivers", "None")], cols=[("Calc_LeverPeople", "User")],
              label_marks=True, hide_labels=["rows"], sort=(("drivers", "None"), ("Calc_LeverPeople", "User"), "DESC"),
              axes=[("Calc_LeverPeople", "User", "cols", "", None)]),
        Sheet("At-risk employees", "Employee", "Text",
              rows=[("emp_id", "None"), ("department", "None"), ("level", "None"), ("drivers", "None")],
              text=[("risk_score", "Sum")], extra_filters=[("risk_band", "High")],
              sort=(("emp_id", "None"), ("risk_score", "Sum"), "DESC"),
              widths=[("emp_id", "None", 90), ("department", "None", 180), ("level", "None", 60), ("drivers", "None", 480)]),
    ]


# ---------------------------------------------------------------- dashboard
W, H = 1440, 900


def z(x, y, w, h):
    """Pixel box to Tableau's 100000-unit zone coordinates."""
    return f"x='{round(x * 100000 / W)}' y='{round(y * 100000 / H)}' w='{round(w * 100000 / W)}' h='{round(h * 100000 / H)}'"


def text_zone(zid, box, runs):
    body = "".join(f"<run bold='{b}' fontcolor='{c}' fontname='{fn}' fontsize='{s}'>{escape(t)}</run>" + ("<run>Æ&#10;</run>" if nl else "")
                   for t, fn, s, c, b, nl in runs)
    return f"""          <zone {z(*box)} id='{zid}' type-v2='text'>
            <formatted-text>{body}</formatted-text>
{ZONE_STYLE}
          </zone>"""


ZONE_STYLE = """            <zone-style>
              <format attr='border-color' value='#000000' />
              <format attr='border-style' value='none' />
              <format attr='border-width' value='0' />
              <format attr='margin' value='4' />
            </zone-style>"""

# Outer gutter of the whole dashboard. Tiled zones stretch to fill their container, so gaps written into the
# zone coordinates are absorbed; the gutter has to be the root container's margin (Tableau's default is 8).
# 20 here plus each zone's own 4 puts content 24 px from the canvas edge.
GUTTER = 20
ROOT_STYLE = f"""          <zone-style>
            <format attr='border-color' value='#000000' />
            <format attr='border-style' value='none' />
            <format attr='border-width' value='0' />
            <format attr='margin' value='4' />
            <format attr='margin-top' value='{GUTTER}' />
            <format attr='margin-right' value='{GUTTER}' />
            <format attr='margin-bottom' value='{GUTTER}' />
            <format attr='margin-left' value='{GUTTER}' />
          </zone-style>"""


def sheet_zone(zid, box, sheet):
    return f"""          <zone {z(*box)} id='{zid}' name={q(sheet)} show-title='false'>
{ZONE_STYLE}
          </zone>"""


def dashboard():
    zones, zid = [], [10]

    def nid():
        zid[0] += 1
        return zid[0]

    zones.append(text_zone(nid(), (24, 14, 800, 36), [("HR analytics workbench", SERIF, 22, TEXT, "false", False)]))
    zones.append(text_zone(nid(), (24, 52, 800, 22), [
        ("Synthetic IT services firm, about 2,500 people, Hyderabad and Bengaluru, April 2022 to March 2026",
         SANS, 9, MUTED, "false", False)]))
    for i, (pid, cap, _) in enumerate(PARAMS):
        zones.append(f"          <zone {z(860 + i * 140, 20, 132, 56)} id='{nid()}' mode='compact' param='[Parameters].[{pid}]' type-v2='paramctrl'>\n"
                     f"{ZONE_STYLE}\n          </zone>")
    kpis = [("KPI attrition", "Attrition, annualised"), ("KPI voluntary", "Voluntary share of exits"),
            ("KPI time to hire", "Median time to hire, days"), ("KPI eNPS", "eNPS"),
            ("KPI pay ratio", "Pay ratio, women to men")]
    for i, (sheet, label) in enumerate(kpis):
        x = 24 + i * 280
        zones.append(text_zone(nid(), (x, 92, 260, 22), [(label, SANS, 9, MUTED, "false", False)]))
        if sheet != "KPI pay ratio":
            zones.append(sheet_zone(nid(), (x, 112, 260, 56), sheet))
            continue
        # The adjusted ratio is one firm-wide figure from the regression: it does not follow the filters
        zones.append(sheet_zone(nid(), (x, 112, 112, 56), sheet))
        zones.append(text_zone(nid(), (x + 112, 116, 148, 48), [
            ("Adjusted, firm-wide", SANS, 9, MUTED, "false", True),
            (f"{ADJUSTED_PAY_RATIO:.1%}", SERIF, 14, TEXT, "false", False)]))
    grid = [("Attrition trend", "Attrition rate, rolling 12 months"), ("Flight risk", "Flight-risk score, current employees"),
            ("Women by level", f"Women's share by level, current employees (levels under {MIN_N} people hidden)"),
            ("Pay vs rating", "Pay vs market by rating, current rated employees"),
            ("Time to hire", f"Median days to hire, by level (levels under {MIN_N} hires hidden)"), ("eNPS trend", "eNPS by survey")]
    for i, (sheet, title) in enumerate(grid):
        col, row = i % 3, i // 3
        x, y = 24 + col * 464, 184 + row * 236
        zones.append(text_zone(nid(), (x, y, 448, 22), [(title, SANS, 10, TEXT, "false", False)]))
        zones.append(sheet_zone(nid(), (x, y + 24, 448, 200), sheet))
    # Bottom row on the same three-column grid: the table spans columns 1-2, the lever summary column 3
    zones.append(text_zone(nid(), (24, 660, 912, 22), [("Highest flight risk: top 10% by model score, with the levers behind each score", SANS, 10, TEXT, "false", False)]))
    zones.append(sheet_zone(nid(), (24, 684, 912, 200), "At-risk employees"))
    zones.append(text_zone(nid(), (952, 660, 448, 22), [("What drives the top 10%: people per lever", SANS, 10, TEXT, "false", False)]))
    zones.append(sheet_zone(nid(), (952, 684, 448, 200), "Risk levers"))
    return f"""    <dashboard name='Workbench'>
      <style>
        <style-rule element='table'>
          <format attr='background-color' value='{PAPER}' />
        </style-rule>
      </style>
      <size maxheight='{H}' maxwidth='{W}' minheight='{H}' minwidth='{W}' />
      <datasources>
        <datasource name='Parameters' />
      </datasources>
{param_dependencies(6, members=True)}
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
{chr(10).join(zones)}
{ROOT_STYLE}
        </zone>
      </zones>
      {simple_id('dashboard', 'Workbench')}
    </dashboard>"""


CARDS = """      <cards>
        <edge name='left'>
          <strip size='160'>
            <card type='pages' />
            <card type='filters' />
            <card type='marks' />
          </strip>
        </edge>
        <edge name='top'>
          <strip size='2147483647'>
            <card type='columns' />
          </strip>
          <strip size='2147483647'>
            <card type='rows' />
          </strip>
        </edge>
      </cards>"""


ADJUSTED_PAY_RATIO = 0.0   # from the pay-equity regression, read at build time


def build(include_dashboard=True, only=None):
    global ADJUSTED_PAY_RATIO
    con = duckdb.connect(str(ROOT / "data" / "hr.duckdb"), read_only=True)
    cols = [(c, TYPE_MAP[t.split("(")[0]]) for c, t, *_ in con.sql("DESCRIBE hr_workbench").fetchall()]
    ADJUSTED_PAY_RATIO = con.sql("SELECT value FROM pay_equity WHERE metric LIKE 'Adjusted%'").fetchone()[0]
    con.close()
    PARAM_VALUES.update(param_values())
    for c, t in cols:
        COLTYPES[c] = (t, "measure" if t in ("real", "integer") else "dimension",
                       "quantitative" if t in ("real", "integer") else "ordinal" if t == "date" else "nominal")
    for name, (cap, dt, role, typ, _) in CALCS.items():
        COLTYPES[name] = (dt, role, typ)
    ws = [x for x in sheets() if not only or x.name in only]
    windows = [f"""    <window class='worksheet' name={q(s.name)}>
{CARDS}
      {simple_id('window', s.name)}
    </window>""" for s in ws]
    dash = ""
    if include_dashboard:
        dash = f"  <dashboards>\n{dashboard()}\n  </dashboards>\n"
        viewpoints = "".join(f"\n        <viewpoint name={q(s.name)} />" for s in ws)
        windows.append(f"""    <window class='dashboard' maximized='true' name='Workbench'>
      <viewpoints>{viewpoints}
      </viewpoints>
      <active id='-1' />
      {simple_id('window', 'Workbench')}
    </window>""")
    manifest = "".join(f"    <{m} />\n" for m in MANIFEST)
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook {HEADER} xmlns:user='http://www.tableausoftware.com/xml/user'>
  <document-format-change-manifest>
{manifest}  </document-format-change-manifest>
  <preferences />
  <style-theme name='clean' />
  <style>
    <style-rule element='animation'>
      <format attr='animation-on' value='ao-off' />
    </style-rule>
  </style>
  <datasources>
{parameters_datasource()}
{datasource(cols)}
  </datasources>
  <worksheets>
{chr(10).join(s.xml() for s in ws)}
  </worksheets>
{dash}  <windows source-height='30'>
{chr(10).join(windows)}
  </windows>
</workbook>
"""


def package(out, include_dashboard=True, only=None):
    twb = build(include_dashboard, only)
    hyper = ROOT / ".captures" / "hr_workbench.hyper"
    make_extract.write_extract(hyper)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(Path(out).with_suffix(".twb").name, twb)
        zf.write(hyper, "Data/Extracts/hr_workbench.hyper")
        zf.write(ROOT / "data" / "model" / "hr_workbench.csv", "Data/hr_workbench/hr_workbench.csv")
    return twb


def main():
    ap = argparse.ArgumentParser(description="Generate the packaged Tableau workbook.")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-dashboard", action="store_true", help="worksheets only (for diagnosing load problems)")
    ap.add_argument("--sheets", nargs="*", help="only these sheets (test switch)")
    ap.add_argument("--filters", choices=["all", "type", "none"], default="all", help="test switch")
    ap.add_argument("--preset", nargs="*", default=[], metavar="CAPTION=VALUE",
                    help="test switch: start a filter on a value, e.g. Department='Data and Analytics'")
    args = ap.parse_args()
    global FILTER_MODE
    FILTER_MODE = args.filters
    by_caption = {cap: pid for pid, cap, _ in PARAMS}
    for item in args.preset:
        cap, val = item.split("=", 1)
        PRESET[by_caption[cap]] = val
    package(args.out, not args.no_dashboard, args.sheets)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
