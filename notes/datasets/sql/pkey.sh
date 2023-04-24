# 2023-04-24 16:36

# notes/docker.sh           Start docker compose
# notes/postgres.sh         Reset database

INSTANCE=datasets/sql/pkey
DATASET=$INSTANCE
# notes/spinta/server.sh    Configure server

cat > $BASEDIR/manifest.txt <<EOF
d | r | b | m | property     | type    | ref         | level | access
$DATASET                     |         |             |       |
  |   |   | Country          |         |             |       |
  |   |   |   | id           | integer |             | 4     | open
  |   |   |   | name         | string  |             | 4     | open
  |   |   | City             |         |             |       |
  |   |   |   | name         | string  |             | 4     | open
  |   |   |   | country      | ref     | Country     | 3     | open
  |   |   | CityExplicit     |         |             |       |
  |   |   |   | name         | string  |             | 4     | open
  |   |   |   | country      | ref     | Country[id] | 3     | open
EOF
poetry run spinta copy $BASEDIR/manifest.txt -o $BASEDIR/manifest.csv
cat $BASEDIR/manifest.csv
poetry run spinta show

# notes/spinta/server.sh    Run migrations
# notes/spinta/server.sh    Run server
# notes/spinta/client.sh    Configure client

uuidgen
#| bf8a4ed3-7db8-4ceb-b8da-7b35ed374149

http POST "$SERVER/$DATASET/Country" $AUTH <<'EOF'
{
    "_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149",
    "id": 42,
    "name": "Lithuania"
}
EOF
#| HTTP/1.1 201 Created
#| 
#| {
#|     "_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149",
#|     "_revision": "e51f8ab0-0a57-4f17-b3c5-692e1a59c5e5",
#|     "_type": "datasets/sql/pkey/Country",
#|     "id": 42,
#|     "name": "Lithuania"
#| }

http POST "$SERVER/$DATASET/City" $AUTH <<'EOF'
{
    "name": "Vilnius",
    "country": {"_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149"}
}
EOF
#| HTTP/1.1 201 Created
#| 
#| {
#|     "_id": "22c7a1af-a995-443c-9182-84d57df2676b",
#|     "_revision": "f72479e9-53b8-4e48-8e0a-8e9846cfb3a0",
#|     "_type": "datasets/sql/pkey/City",
#|     "country": {
#|         "_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149"
#|     },
#|     "name": "Vilnius"
#| }


http POST "$SERVER/$DATASET/City" $AUTH <<'EOF'
{
    "name": "Kaunas",
    "country": {"name": "Lithuania"}
}
EOF
#| HTTP/1.1 400 Bad Request
#| 
#| {
#|     "errors": [
#|         {
#|             "code": "FieldNotInResource",
#|             "context": {
#|                 "attribute": "",
#|                 "component": "spinta.components.Property",
#|                 "dataset": "datasets/sql/pkey",
#|                 "entity": "",
#|                 "manifest": "default",
#|                 "model": "datasets/sql/pkey/City",
#|                 "property": "name",
#|                 "schema": "8"
#|             },
#|             "message": "Unknown property 'name'.",
#|             "template": "Unknown property {property!r}.",
#|             "type": "property"
#|         }
#|     ]
#| }

http GET "$SERVER/$DATASET/City?select(_id,country)&format(ascii)"
#| _id                                   country._id                         
#| ------------------------------------  ------------------------------------
#| 22c7a1af-a995-443c-9182-84d57df2676b  bf8a4ed3-7db8-4ceb-b8da-7b35ed374149


http POST "$SERVER/$DATASET/CityExplicit" $AUTH <<'EOF'
{
    "name": "Vilnius",
    "country": {"_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149"}
}
EOF
#| HTTP/1.1 201 Created
#| 
#| {
#|     "_id": "c3e3c7b6-683d-4fd0-8e54-4e5e504f8d7a",
#|     "_revision": "d0fff86e-16c4-4ccd-b5a3-d3f89fbfc34a",
#|     "_type": "datasets/sql/pkey/CityExplicit",
#|     "country": {
#|         "_id": "bf8a4ed3-7db8-4ceb-b8da-7b35ed374149"
#|     },
#|     "name": "Vilnius"
#| }
# FIXME: This should be an error, _id is not allowed.


http POST "$SERVER/$DATASET/CityExplicit" $AUTH <<'EOF'
{
    "name": "Kaunas",
    "country": {"id": 42}
}
EOF
#| HTTP/1.1 400 Bad Request
#| 
#| {
#|     "errors": [
#|         {
#|             "code": "FieldNotInResource",
#|             "context": {
#|                 "attribute": "",
#|                 "component": "spinta.components.Property",
#|                 "dataset": "datasets/sql/pkey",
#|                 "entity": "",
#|                 "manifest": "default",
#|                 "model": "datasets/sql/pkey/CityExplicit",
#|                 "property": "id",
#|                 "schema": "12"
#|             },
#|             "message": "Unknown property 'id'.",
#|             "template": "Unknown property {property!r}.",
#|             "type": "property"
#|         }
#|     ]
#| }
# FIXME: This should be allowed.


http GET "$SERVER/$DATASET/CityExplicit?select(_id,country)&format(ascii)"
#| _id                                   country._id                         
#| ------------------------------------  ------------------------------------
#| c3e3c7b6-683d-4fd0-8e54-4e5e504f8d7a  bf8a4ed3-7db8-4ceb-b8da-7b35ed374149
# FIXME: I expect to get this:
#
#     _id                                   country.id                         
#     ------------------------------------  ----------
#     c3e3c7b6-683d-4fd0-8e54-4e5e504f8d7a  42
