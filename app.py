"""Monthly bilateral real exchange rates using IMF SDMX 3.0."""
from io import StringIO
import base64
from html import escape
import xml.etree.ElementTree as ET
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

BASE = 'https://api.imf.org/external/sdmx/3.0'
START = '1976-01'


def get(url, accept, params=None):
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    with session:
        response = session.get(url, params=params, headers={'Accept': accept}, timeout=(10, 45))
    response.raise_for_status()
    return response


def local(tag):
    return tag.rsplit('}', 1)[-1]


@st.cache_data(ttl=86400, show_spinner=False)
def countries():
    """Use IMF's JSON country codelist; XML 2.1 is not supported here."""
    response = get(f'{BASE}/structure/dataflow/IMF.STA/CPI',
                   'application/json',
                   {'references': 'all'})
    payload = response.json()['data']
    result = {}
    for codelist in payload.get('codelists', []):
        if codelist.get('id') != 'CL_COUNTRY':
            continue
        for code in codelist.get('codes', []):
            iso = code['id']
            if pycountry.countries.get(alpha_3=iso) or iso == 'G163':
                result[iso] = code.get('names', {}).get('en', code.get('name', iso))
    if not result:
        raise ValueError('IMF country metadata could not be read.')
    result['G163'] = 'Euro area'
    return result


def parse_csv(text):
    frame = pd.read_csv(StringIO(text), dtype=str)
    frame.columns = frame.columns.str.strip().str.upper()
    if not {'TIME_PERIOD', 'OBS_VALUE'}.issubset(frame.columns):
        raise ValueError('IMF response is not an observation CSV with TIME_PERIOD and OBS_VALUE.')
    original_periods = frame.TIME_PERIOD.head(5).tolist()
    frame['TIME_PERIOD'] = frame.TIME_PERIOD.str.replace(r'^(\d{4})-M(\d{2})$', r'\1-\2', regex=True).str.replace(r'^(\d{4}-\d{2})-01(?:T.*)?$', r'\1', regex=True)
    frame = frame[frame.TIME_PERIOD.str.fullmatch(r'\d{4}-\d{2}', na=False)].copy()
    if frame.empty:
        raise ValueError(f'No recognized monthly periods. IMF examples: {original_periods}; response: {text[:600]}')
    frame['month'] = pd.to_datetime(frame.TIME_PERIOD, format='%Y-%m')
    frame['value'] = pd.to_numeric(frame.OBS_VALUE, errors='coerce')
    # A series-specific multiplier, if supplied, scales the observation level.
    scale_column = 'SCALE' if 'SCALE' in frame else 'UNIT_MULT'
    if scale_column in frame:
        multiplier = pd.to_numeric(frame[scale_column], errors='coerce').fillna(0)
        frame['value'] *= 10.0 ** multiplier
    if frame.month.duplicated().any():
        raise ValueError('IMF returned multiple observations per month; inspect the series key.')
    return frame.set_index('month')['value'].sort_index()


@st.cache_data(ttl=21600, show_spinner=False)
def fetch(iso, kind):
    flow, key = ('CPI', f'{iso}.CPI._T.IX.M') if kind == 'cpi' else ('ER', f'{iso}.XDC_USD.PA_RT.M')
    url = f'{BASE}/data/dataflow/IMF.STA/{flow}/+/{key}'
    response = get(url, 'application/vnd.sdmx.data+csv;version=2.0.0',
                   {'startPeriod': START, 'endPeriod': pd.Timestamp.today().strftime('%Y-%m')})
    if response.status_code == 204 or not response.text.strip():
        return pd.Series(dtype=float, name=kind)
    # IMF may return the full history even when legacy date parameters are supplied.
    series = parse_csv(response.text)
    return series.loc[START:pd.Timestamp.today().strftime('%Y-%m')]


def calculate(inputs):
    """Do not fill gaps. Compute the reference mean only on the chosen sample."""
    out = inputs.copy()
    required = ['cpi1', 'cpi2', 'fx1_lcu_per_usd', 'fx2_lcu_per_usd']
    for col in required:
        out[col] = pd.to_numeric(out[col], errors='coerce')
        out[col] = out[col].where((out[col] > 0) & (out[col] < float('inf')))
    out['nominal_s'] = out.fx2_lcu_per_usd / out.fx1_lcu_per_usd
    out['rer'] = out.nominal_s * out.cpi1 / out.cpi2
    out['valid_rer'] = out[required].notna().all(axis=1) & out.rer.notna()
    reference = out.loc[out.valid_rer, 'rer'].mean()
    out['rer_reference_mean'] = reference
    out['real_index'] = 100 * out.rer / reference
    return out


def chart(data, mode, units):
    fig = make_subplots(specs=[[{'secondary_y': True}]])
    if mode in ('Real', 'Both'):
        fig.add_trace(go.Scatter(x=data.index, y=data.real_index, name='Real index',
                      connectgaps=False, line={'color': '#196f61', 'width': 2.8}), secondary_y=False)
        fig.add_hline(y=100, line_dash='dot', line_color='#98A2B3')
    if mode in ('Nominal', 'Both'):
        fig.add_trace(go.Scatter(x=data.index, y=data.nominal_s, name=f'Nominal S ({units})',
                      connectgaps=False, line={'color': '#c28453', 'width': 2.4}), secondary_y=(mode == 'Both'))
    fig.update_yaxes(title_text='Real index (sample mean = 100)' if mode != 'Nominal'
                     else units, secondary_y=False)
    if mode == 'Both':
        fig.update_yaxes(title_text=units, secondary_y=True)
    fig.update_layout(template='plotly_white', hovermode='x unified', height=470,
                      paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                      font={'family': 'Arial, sans-serif', 'color': '#4d6665', 'size': 12},
                      legend={'orientation': 'h', 'y': 1.16, 'font': {'size': 12}},
                      margin={'t': 65, 'b': 45, 'l': 58, 'r': 58})
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor='#e6ece7', zeroline=False)
    fig.update_yaxes(showgrid=False, secondary_y=True)
    return fig



@st.cache_data(ttl=86400, show_spinner=False)
def flag_image(iso):
    """Embed small flag assets so page rendering does not depend on a CDN request."""
    country = pycountry.countries.get(alpha_3=iso)
    code = 'eu' if iso == 'G163' else (country.alpha_2.lower() if country else '')
    if not code:
        return ''
    try:
        response = requests.get(f'https://flagcdn.com/w320/{code}.png', timeout=(3, 5))
        response.raise_for_status()
        return 'data:image/png;base64,' + base64.b64encode(response.content).decode('ascii')
    except requests.RequestException:
        return ''


def visual_theme(one, two):
    left, right = flag_image(one), flag_image(two)
    st.markdown('''<style>
    :root {color-scheme:light;}
    [data-testid="stAppViewContainer"] {background:#f5f5f0;color:#172d32;}
    [data-testid="stAppViewContainer"]::before,
    [data-testid="stAppViewContainer"]::after {
        content:"";position:fixed;width:58vw;height:74vh;top:0;pointer-events:none;
        background-repeat:no-repeat;background-position:center;background-size:cover;
        opacity:.095;filter:blur(8px);z-index:0;
        mask-image:linear-gradient(to bottom,black,transparent 94%);
        -webkit-mask-image:linear-gradient(to bottom,black,transparent 94%);
    }
    [data-testid="stAppViewContainer"]::before {left:-13vw;transform:rotate(-12deg);}
    [data-testid="stAppViewContainer"]::after {right:-13vw;transform:rotate(12deg);}
    [data-testid="stHeader"] {background:transparent;}
    [data-testid="stMain"] {position:relative;z-index:1;}
    .block-container {max-width:1160px;padding-top:3.3rem;padding-bottom:3rem;}
    .rer-eyebrow {font-size:11px;font-weight:700;letter-spacing:.19em;color:#326c65;
        text-transform:uppercase;margin-bottom:15px;}
    .rer-hero h1 {font-size:clamp(38px,5.2vw,65px);font-weight:650;letter-spacing:-.055em;
        line-height:1.05;color:#173b3a;padding:0;margin:0 0 18px;}
    .rer-hero p {font-size:16px;line-height:1.65;color:#52696b;max-width:610px;margin:0;}
    .rer-hero {padding:4px 0 28px;}
    .rer-pair {display:grid;grid-template-columns:1fr 36px 1fr;align-items:center;gap:12px;
        margin:12px 0 20px;}
    .rer-country {display:flex;align-items:center;gap:14px;padding:19px 21px;
        min-height:93px;background:rgba(255,255,255,.82);border:1px solid #dce5e0;
        border-radius:18px;box-shadow:0 8px 28px #203c3510;}
    .rer-flag {width:62px;height:42px;object-fit:cover;border-radius:7px;
        box-shadow:0 2px 8px #173b3a18;flex-shrink:0;}
    .rer-country small {display:block;text-transform:uppercase;font-size:10px;
        letter-spacing:.13em;color:#728482;margin-bottom:5px;}
    .rer-country strong {font-size:18px;font-weight:650;color:#173b3a;line-height:1.2;}
    .rer-arrow {color:#66877e;text-align:center;font-size:24px;}
    .rer-stat {background:rgba(255,255,255,.87);border:1px solid #dce5e0;
        padding:18px 20px;border-radius:15px;margin:0 0 12px;min-height:94px;}
    .rer-stat small {font-size:11px;color:#6d817c;letter-spacing:.055em;display:block;
        margin-bottom:7px;}
    .rer-stat strong {font-size:27px;color:#173b3a;font-weight:600;letter-spacing:-.035em;}
    [data-testid="stMarkdownContainer"] p,[data-testid="stCaptionContainer"] p {
        color:#4d6665;}
    [data-testid="stWidgetLabel"] p {color:#234a46;font-weight:600;font-size:13px;}
    div[data-baseweb="select"]>div {background:#ffffffed!important;color:#173b3a!important;
        border-color:#d7e1db!important;border-radius:12px!important;}
    input[role="combobox"] {color:#173b3a!important;}
    [data-testid="stRadio"] label {background:#ffffffba;border:1px solid #dbe4df;
        border-radius:999px;padding:7px 13px!important;}
    [data-testid="stRadio"] label p {color:#214b45!important;}
    [data-testid="stButton"] button {border-radius:10px;background:#ffffffb3;
        border:1px solid #cbdad3;color:#326c65;font-size:12px;}
    [data-testid="stDownloadButton"] button {background:#164f49!important;color:#fff!important;
        border:0;border-radius:12px;padding:12px 22px;box-shadow:0 5px 15px #164f4920;}
    [data-testid="stDownloadButton"] button p {color:white!important;}
    [data-testid="stPlotlyChart"] {background:#ffffffed;border:1px solid #dce5e0;
        border-radius:20px;overflow:hidden;padding:8px 4px;box-shadow:0 12px 40px #203c3507;}
    [data-testid="stExpander"] {background:#ffffffc4;border-radius:14px;border-color:#dce5e0;}
    [data-testid="stAlert"] {background:#e9f0e9;color:#33524c;border-radius:14px;}
    [data-testid="stSlider"] [role="slider"] {background:#1b6c60!important;}
    @media(max-width:700px) {
        .block-container {padding-top:2.4rem;padding-left:1.1rem;padding-right:1.1rem;}
        .rer-country {padding:13px 12px;gap:9px;border-radius:13px;min-height:86px;}
        .rer-flag {width:42px;height:30px;}
        .rer-country strong {font-size:14px;}
        .rer-country small {font-size:9px;}
        .rer-pair {gap:5px;grid-template-columns:1fr 22px 1fr;}
        .rer-stat {padding:14px 12px;min-height:88px;}
        .rer-stat strong {font-size:21px;}
    }
    </style>''' + f'''<style>
    [data-testid="stAppViewContainer"]::before {{background-image:url("{left}");}}
    [data-testid="stAppViewContainer"]::after {{background-image:url("{right}");}}
    </style>''', unsafe_allow_html=True)
    return left, right


def pair_header(names, one, two, left, right):
    def card(code, image, role):
        flag = f'<img class="rer-flag" src="{image}" alt="{escape(names[code])} flag">' if image else ''
        return f'<div class="rer-country">{flag}<div><small>{role} · {code}</small><strong>{escape(names[code])}</strong></div></div>'
    return ('<div class="rer-pair">' + card(one, left, 'Country 1') +
            '<div class="rer-arrow">↔</div>' + card(two, right, 'Country 2') + '</div>')



def main():
    st.set_page_config(page_title='Real Exchange Rate Explorer', page_icon='🌐', layout='wide')
    hero = st.empty()
    try:
        names = countries()
        metadata_warning = None
    except Exception as exc:
        names = {c.alpha_3: c.name for c in pycountry.countries}
        names['G163'] = 'Euro area'
        metadata_warning = str(exc)
    codes = sorted(names, key=lambda code: names[code])
    if metadata_warning:
        st.warning('IMF country metadata is unavailable. Showing ISO countries plus Euro area; some have no IMF series.')
        with st.expander('Metadata error'):
            st.code(metadata_warning)
    a, b = st.columns(2)
    one = a.selectbox('Country 1', codes, index=codes.index('GBR'), format_func=lambda x: f'{names[x]} ({x})')
    two = b.selectbox('Country 2', codes, index=codes.index('USA'), format_func=lambda x: f'{names[x]} ({x})')
    left, right = visual_theme(one, two)
    hero.markdown('<div class="rer-hero"><div class="rer-eyebrow">Currency explorer / IMF monthly data</div><h1>Real exchange rates.</h1><p>See how currencies and purchasing power move together. Choose two countries to explore their relationship over time.</p></div>' + pair_header(names, one, two, left, right), unsafe_allow_html=True)
    units = f'{names[two]} currency per 1 {names[one]} currency'
    if (one, two) == ('GBR', 'USA'):
        units = 'USD per GBP'
    st.caption(f'Nominal S: {units}. IMF inputs are local currency per USD (monthly period averages).')
    end = pd.Timestamp.today().to_period('M').to_timestamp()
    months = pd.date_range(START, end, freq='MS')
    sample = st.select_slider('Sample period', options=list(months),
                             value=(pd.Timestamp('2000-01-01'), end),
                             format_func=lambda x: x.strftime('%Y-%m'))
    mode = st.radio('Chart', ['Real', 'Nominal', 'Both'], index=2, horizontal=True)
    if st.button('Retry / refresh IMF data'):
        st.cache_data.clear()
        st.rerun()
    try:
        with st.spinner('Downloading monthly IMF series…'):
            cpi1 = fetch(one, 'cpi')
            cpi2 = fetch(two, 'cpi')
            fx1 = pd.Series(1.0, index=months) if one == 'USA' else fetch(one, 'fx')
            fx2 = pd.Series(1.0, index=months) if two == 'USA' else fetch(two, 'fx')
        inputs = pd.concat([cpi1.rename('cpi1'), cpi2.rename('cpi2'),
                            fx1.rename('fx1_lcu_per_usd'), fx2.rename('fx2_lcu_per_usd')], axis=1)
        inputs = inputs.reindex(pd.date_range(sample[0], sample[1], freq='MS'))
        inputs.index.name = 'month'
        data = calculate(inputs)
    except Exception as exc:
        st.error('IMF data could not be downloaded. Retry later. No substitute or simulated data are displayed.')
        st.code(f'{type(exc).__name__}: {exc}')
        st.info('If this persists, copy the error above and the three repository files when asking for help.')
        return
    count = int(data.valid_rer.sum())
    st.caption(f'{count} valid real-rate months out of {len(data)} selected months. Missing observations are left blank; no interpolation.')
    if not count:
        st.warning('No overlapping CPI and FX observations in this period. Try another period or country pair.')
    else:
        a, b, c = st.columns(3)
        a.markdown(f'<div class="rer-stat"><small>REFERENCE RER</small><strong>{data.rer_reference_mean.iloc[0]:.5f}</strong></div>', unsafe_allow_html=True)
        b.markdown(f'<div class="rer-stat"><small>MEAN REAL INDEX</small><strong>{data.real_index.mean():.2f}</strong></div>', unsafe_allow_html=True)
        c.markdown(f'<div class="rer-stat"><small>MONTHS OF DATA</small><strong>{count}</strong></div>', unsafe_allow_html=True)
    st.plotly_chart(chart(data, mode, units), use_container_width=True)
    export = data.copy()
    export.insert(0, 'country2', two)
    export.insert(0, 'country1', one)
    export['nominal_units'] = units
    export['source'] = 'IMF SDMX 3.0 CPI / ER; USA FX = 1 by definition'
    export['retrieved_utc'] = pd.Timestamp.now(tz='UTC').isoformat()
    st.download_button('Download sample CSV', export.to_csv(index=True, date_format='%Y-%m').encode('utf-8'),
                       file_name=f'rer_{one}_{two}_{sample[0]:%Y%m}_{sample[1]:%Y%m}.csv', mime='text/csv')
    with st.expander('Observations and manual verification', expanded=False):
        st.dataframe(export, use_container_width=True)
        if count:
            t = data.index[data.valid_rer][0]
            row = data.loc[t]
            st.write(f'{t:%Y-%m}: S = {row.fx2_lcu_per_usd:.10g} / {row.fx1_lcu_per_usd:.10g} = {row.nominal_s:.10g}. '
                     f'RER = {row.nominal_s:.10g} × {row.cpi1:.10g} / {row.cpi2:.10g} = {row.rer:.10g}. '
                     f'Real index = 100 × {row.rer:.10g} / {row.rer_reference_mean:.10g} = {row.real_index:.10g}.')
    with st.expander('How to read the chart'):
        st.latex(r'S_t=FX_{2,t}/FX_{1,t},\quad RER_t=S_t P_{1,t}/P_{2,t}')
        st.latex(r'RealIndex_t=100 RER_t/\overline{RER}')
    st.info('An increase means Country 1 appreciates in real terms under this definition. '
            'The sample average is a reference, not an estimated equilibrium or proof of overvaluation. '
            'CPI base years affect raw RER levels but cancel in the real index. Currency reforms can break historical comparability; inspect IMF metadata before interpreting long histories.')
    st.caption('Euro area uses IMF code G163. Coverage may start after 1976. No pre-euro exchange rates are fabricated.')


if __name__ == '__main__':
    main()
