import datetime
import hashlib

import pytest


def sha1(s):
    return hashlib.sha1(s.encode()).hexdigest()


@pytest.mark.skip('datasets')
def test_export_csv(context, app, mocker):
    mocker.patch('spinta.backends.postgresql.sqlalchemy.utcnow', return_value=datetime.datetime(2019, 3, 6, 16, 15, 0, 816308))

    app.authorize(['spinta_set_meta_fields'])
    app.authmodel('country/:dataset/csv/:resource/countries', ['upsert', 'getall', 'search', 'changes'])

    resp = app.post('/country/:dataset/csv/:resource/countries', json={'_data': [
        {
            '_op': 'upsert',
            '_type': 'country/:dataset/csv/:resource/countries',
            '_id': sha1('1'),
            '_where': '_id="' + sha1('1') + '"',
            'code': 'lt',
            'title': 'Lithuania',
        },
        {
            '_op': 'upsert',
            '_type': 'country/:dataset/csv/:resource/countries',
            '_id': sha1('2'),
            '_where': '_id="' + sha1('2') + '"',
            'code': 'lv',
            'title': 'LATVIA',
        },
        {
            '_op': 'upsert',
            '_type': 'country/:dataset/csv/:resource/countries',
            '_id': sha1('2'),
            '_where': '_id="' + sha1('2') + '"',
            'code': 'lv',
            'title': 'Latvia',
        },
    ]})
    assert resp.status_code == 200, resp.json()

    data = app.get('/country/:dataset/csv/:resource/countries?sort(+code)').json()['_data']
    ids = [d['_id'] for d in data]
    rev = [d['_revision'] for d in data]
    assert app.get('/country/:dataset/csv/:resource/countries/:format/csv?sort(+code)').text == (
        '_type,_id,_revision,code,title\r\n'
        f'country/:dataset/csv/:resource/countries,{ids[0]},{rev[0]},lt,Lithuania\r\n'
        f'country/:dataset/csv/:resource/countries,{ids[1]},{rev[1]},lv,Latvia\r\n'
    )

    changes = app.get('/country/:dataset/csv/:resource/countries/:changes').json()['_data']
    ids = [c['_rid'] for c in changes]
    cxn = [c['_id'] for c in changes]
    txn = [c['_txn'] for c in changes]
    rev = [c['_revision'] for c in changes]
    assert app.get('/country/:dataset/csv/:resource/countries/:changes/:format/csv').text == (
        '_id,_rid,_revision,_txn,_created,_op,code,title\r\n'
        f'{cxn[0]},{ids[0]},{rev[0]},{txn[0]},2019-03-06T16:15:00.816308,upsert,lt,Lithuania\r\n'
        f'{cxn[1]},{ids[1]},{rev[1]},{txn[1]},2019-03-06T16:15:00.816308,upsert,lv,LATVIA\r\n'
        f'{cxn[2]},{ids[2]},{rev[2]},{txn[2]},2019-03-06T16:15:00.816308,upsert,,Latvia\r\n'
    )
