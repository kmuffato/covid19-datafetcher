import csv
from io import StringIO
from copy import copy
from datetime import datetime
from utils import request_and_parse, extract_attributes, \
   map_attributes, Fields

''' This file contains extra handling needed for some states
To make it work, the method must be called "handle_{state_abbreviation:lower_case}"
The parameters are:
- query result
- state mappings
'''

def handle_ct(res, mapping):
    # res is a list of dict, one per day
    if not res:
        return {}
    res = res[0]

    sorted_res = sorted(res, key=lambda x: x['date'], reverse = True)
    latest = sorted_res[0]
    mapped = map_attributes(latest, mapping, 'CT')
    return mapped

def handle_fl(res, mapping):
    '''Need to add the non-FL residents to the totals:
    they separate it for death and hosp"
    '''
    res = res[0]
    mapped = extract_attributes(res, mapping, 'FL')
    extra_hosp = 0
    extra_death = 0
    try:
        extra_hosp = res['features'][0]['attributes']['SUM_C_HospYes_NonRes']
        extra_death = res['features'][0]['attributes']['SUM_C_NonResDeaths']
    except Exception as ex:
        print("Failed Florida extra processing: ", str(e))
        raise

    mapped[Fields.HOSP.name] += extra_hosp
    mapped[Fields.DEATH.name] += extra_death

    return mapped

def handle_vt(res, mapping):
    state = 'VT'
    tagged = {}
    updated_mapping = copy(mapping)
    pui = 'hosp_pui'
    updated_mapping.update({pui: pui})
    for result in res:
        partial = extract_attributes(result, updated_mapping, state)
        tagged.update(partial)

    tagged[Fields.CURR_HOSP.name] += tagged[pui]
    tagged.pop(pui)
    return tagged

def handle_pa(res, mapping):
    '''PA has different data sources for positive/death and for hospitalization that's displayd on the dashboard
    The main source has a different number for hosp.

    Also need to sub ECMO to Vent number
    '''
    state = 'PA'
    tagged = {}
    updated_mapping = copy(mapping)
    ecmo = 'ecmo'
    updated_mapping.update({ecmo: ecmo})
    for result in res:
        partial = extract_attributes(result, updated_mapping, state)
        tagged.update(partial)

    tagged[Fields.CURR_VENT.name] += tagged[ecmo]
    tagged.pop(ecmo)
    return tagged

def handle_nm(res, mapping):
    data = res[0]['data']
    mapped = map_attributes(data, mapping, 'NM')
    return mapped

def handle_ne(res, mapping):
    res = res[0]
    tagged = {}
    if 'features' in res and len(res['features']) > 0:
        attributes = res['features']
        for attr in attributes:
            # expecting {attributes: {lab_status: NAME, COUNT_EXPR0: VALUE}}
            name = attr['attributes']['lab_status']
            value = attr['attributes']['COUNT_EXPR0']
            if name in mapping:
                tagged[mapping[name]] = value

    return tagged

def handle_in(res, mapping):
    # ckan
    tagged = {}

    # There's pretty bad error handling now
    # I want to get errors as fast as possible -- to fix faster
    stats = res[0]['result']['records'][0]
    tagged = map_attributes(stats, mapping, 'IN')

    hosp_data = res[1]['result']['records']
    for record in hosp_data:
        name = record['STATUS_TYPE']
        value = record['TOTAL']
        if name in mapping:
            tagged[mapping[name]] = value

    return tagged

def handle_la(res, mapping):
    res = res[0]
    state_tests = 'SUM_State_Tests'
    state = 'LA'
    tagged = extract_attributes(res, mapping, state)
    try:
        # TODO: extract this into a separate function
        val = res['features'][0]['attributes'][state_tests]
        tagged[Fields.TOTAL.name] += val
    except Exception as e:
        print(str(e))
        raise
    return tagged

def handle_il(res, mapping):
    state = 'IL'
    state_name = 'Illinois'
    mapped = {}
    # main dashboard
    for county in res[0]['characteristics_by_county']['values']:
        if county['County'] == state_name:
            mapped = map_attributes(county, mapping, state)

    last_update = res[0]['LastUpdateDate']
    y = last_update['year']
    m = last_update['month']
    d = last_update['day']
    timestamp = datetime(y,m,d).timestamp()
    mapped[Fields.TIMESTAMP.name] = timestamp

    # hospital data
    hosp_data = res[1]['statewideValues']
    hosp_mapped = map_attributes(hosp_data, mapping, state)
    mapped.update(hosp_mapped)

    # fill in the date
    last_update = res[1]['LastUpdateDate']
    y = last_update['year']
    m = last_update['month']
    d = last_update['day']
    updated = datetime(y,m,d, 23, 59).strftime("%m/%d/%Y %H:%M:%S")
    mapped[Fields.DATE.name] = updated

    return mapped

def handle_gu(res, mapping):
    res = res[0]
    tagged = {}
    if 'features' in res and len(res['features']) > 0:
        attributes = res['features']
        for attr in attributes:
            # expecting {attributes: {Variable: NAME, Count: VALUE}}
            name = attr['attributes']['Variable']
            value = attr['attributes']['Count']
            if name in mapping:
                tagged[mapping[name]] = value

    return tagged

def handle_hi(res, mapping):
    res = res[0]

    last_state_row = {}
    for row in res:
        if row['Region'] == 'State':
            last_state_row = row

    tagged = {}
    # expecting the order be old -> new data, so last line is the newest
    for k, v in last_state_row.items():
        if k in mapping:
            tagged[mapping[k]] = v

    return tagged

def handle_ri(res, mapping):
    dict_res = {r[0]: r[1] for r in res[0]}
    mapped = map_attributes(dict_res, mapping, 'RI')
    return mapped

def handle_ca(res, mapping):
    # ckan
    tagged = {}

    stats = res[0]['result']['records'][0]
    tagged = map_attributes(stats, mapping, 'CA')

    # add hosp and icu pui
    tagged[Fields.CURR_HOSP.name] = int(tagged[Fields.CURR_HOSP.name]) + int(stats.get('curr_hosp_pui', 0))
    tagged[Fields.CURR_ICU.name] = int(tagged[Fields.CURR_ICU.name]) + int(stats.get('curr_icu_pui', 0))
    return tagged

def handle_va(res, mapping):
    '''Getting multiple CVS files from the state and parsing each for
    the specific data it contains
    '''
    tagged = {}

    # Res:
    # 0 -- cases & death, probable & confirmed
    # 1 -- testing info
    # 2 -- hospital/icu/vent
    cases = res[0]
    testing = res[1]
    hospital = res[2]

    date_format = "%m/%d/%Y"

    # Cases
    # expecting 2 rows in the following format
    # Report Date,Case Status,Number of Cases,Number of Hospitalizations,Number of Deaths
    # 5/14/2020,Probable,1344,24,28
    # 5/14/2020,Confirmed,26469,3568,927

    PROB = 'Probable'
    CONF = 'Confirmed'

    for row in cases:
        if (row['Case Status'] == CONF):
            for k, v in row.items():
                if (k in mapping):
                    tagged[mapping[k]] = v
        elif (row['Case Status'] == PROB):
            tagged[Fields.PROBABLE.name] = row['Number of Cases']
            tagged[Fields.DEATH_PROBABLE.name] = row['Number of Deaths']


    # Verify that we're only taking the most recent date
    testing = sorted(testing, key=lambda x: datetime.strptime(x['Report Date'],date_format), reverse = True)
    most_recent_date = testing[0]['Report Date']
    testing = [x for x in testing if x['Report Date'] == most_recent_date]
    total_tests = [x['Number of Testing Encounters'] for x in testing]
    total_tests = sum([int(x.replace(',','')) for x in total_tests])
    tagged[Fields.TOTAL.name] = total_tests

    # Hospitalizations
    hospital = sorted(hospital, key=lambda x: datetime.strptime(x['Date'], date_format), reverse = True)
    print("Hospital:")
    print(hospital[0])
    mapped_hosp = map_attributes(hospital[0], mapping, 'VA')
    tagged.update(mapped_hosp)

    return tagged
