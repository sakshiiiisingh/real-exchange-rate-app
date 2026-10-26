"""Monthly bilateral real exchange rates using IMF SDMX 3.0."""
from io import StringIO
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
                      connectgaps=False, line={'color': '#175CD3'}), secondary_y=False)
        fig.add_hline(y=100, line_dash='dot', line_color='#98A2B3')
    if mode in ('Nominal', 'Both'):
        fig.add_trace(go.Scatter(x=data.index, y=data.nominal_s, name=f'Nominal S ({units})',
                      connectgaps=False, line={'color': '#D56A21'}), secondary_y=(mode == 'Both'))
    fig.update_yaxes(title_text='Real index (sample mean = 100)' if mode != 'Nominal'
                     else units, secondary_y=False)
    if mode == 'Both':
        fig.update_yaxes(title_text=units, secondary_y=True)
    fig.update_layout(template='plotly_white', hovermode='x unified', height=480,
                      legend={'orientation': 'h', 'y': 1.12}, margin={'t': 55})
    return fig


def main():
    st.set_page_config(page_title='Real Exchange Rate Explorer', page_icon='🌐', layout='wide')
    st.title('Real Exchange Rate Explorer')
    st.write('Compare monthly prices and exchange rates using IMF data, from January 1976 where available.')
    st.latex(r'S_t=FX_{2,t}/FX_{1,t},\quad RER_t=S_t P_{1,t}/P_{2,t},\quad RealIndex_t=100 RER_t/\overline{RER}')
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
        a.write(f'Reference RER: **{data.rer_reference_mean.iloc[0]:.6g}**')
        b.write(f'Mean real index: **{data.real_index.mean():.8f}**')
        c.write(f'Valid months: **{count}**')
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
    st.info('An increase means Country 1 appreciates in real terms under this definition. '
            'The sample average is a reference, not an estimated equilibrium or proof of overvaluation. '
            'CPI base years affect raw RER levels but cancel in the real index. Currency reforms can break historical comparability; inspect IMF metadata before interpreting long histories.')
    st.caption('Euro area uses IMF code G163. Coverage may start after 1976. No pre-euro exchange rates are fabricated.')


if __name__ == '__main__':
    main()
