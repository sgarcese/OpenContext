# Built-in Plugins Reference

OpenContext includes built-in plugins for CKAN, Socrata, and Opendatasoft open data portals.

## CKAN Plugin

For CKAN-based open data portals (e.g., data.boston.gov, data.gov, data.gov.uk).

### Configuration

```yaml
plugins:
  ckan:
    enabled: true
    base_url: "https://data.yourcity.gov"       # CKAN API base URL
    portal_url: "https://data.yourcity.gov"     # Public portal URL
    city_name: "Your City"                      # City/organization name
    timeout: 120                                # HTTP timeout in seconds
    api_key: "${CKAN_API_KEY}"                  # Optional: API key
```

### Tools

- `ckan__search_datasets(query, limit)` - Search for datasets
- `ckan__get_dataset(dataset_id)` - Get dataset metadata
- `ckan__query_data(resource_id, filters, limit)` - Query data from a resource
- `ckan__get_schema(resource_id)` - Get schema for a resource

### Examples

**Search datasets:**
```
Search for datasets about housing in Boston
```

**Get dataset:**
```
Get details about the "311 Service Requests" dataset
```

**Query data:**
```
Query the first 10 records from resource abc123
```

## CKAN API

This plugin uses CKAN's Action API:
- `/api/3/action/package_search` - Search datasets
- `/api/3/action/package_show` - Get dataset
- `/api/3/action/datastore_search` - Query data

See [CKAN API documentation](https://docs.ckan.org/en/latest/api/) for details.

## Socrata Plugin

For Socrata-based open data portals (e.g., data.cityofchicago.org, data.cityofnewyork.us, data.seattle.gov).

**Note:** Socrata requires a free app token. Register at [https://dev.socrata.com/register](https://dev.socrata.com/register).

### Configuration

```yaml
plugins:
  socrata:
    enabled: true
    base_url: "https://data.cityofboston.gov"
    portal_url: "https://data.cityofboston.gov"
    city_name: "Boston"
    app_token: "${SOCRATA_APP_TOKEN}"   # Required
    timeout: 30.0                        # HTTP timeout (default: 30)
```

### Tools

- `socrata__search_datasets(query, limit)` - Search for datasets in the portal catalog
- `socrata__get_dataset(dataset_id)` - Get full metadata for a dataset (4x4 ID)
- `socrata__get_schema(dataset_id)` - Get column schema for constructing SoQL queries
- `socrata__query_dataset(dataset_id, soql_query)` - Query data using SoQL
- `socrata__execute_sql(dataset_id, soql)` - Execute raw SoQL query (advanced, similar to CKAN execute_sql)
- `socrata__list_categories()` - List all categories with dataset counts

### Examples

**Search datasets:**
```
Search for datasets about housing in Boston
```

**Get dataset:**
```
Get details about dataset wc4w-4jew
```

**Get schema (call before query_dataset):**
```
Get schema for dataset wc4w-4jew
```

**Query data:**
```
Query dataset wc4w-4jew with: SELECT * WHERE year > 2020 LIMIT 50
```

**List categories:**
```
List all dataset categories on Boston's open data portal
```

### Socrata API

This plugin uses two Socrata API layers:
- **Discovery API** (api.us.socrata.com) - Catalog search, categories
- **SODA3** (portal domain) - Dataset metadata, schema, data queries

See [Socrata developer documentation](https://dev.socrata.com/) for details.

## Opendatasoft Plugin

For Opendatasoft-based open data portals using the Explore API v2.1 (e.g., data.longbeach.gov, public.opendatasoft.com).

**Note:** Public Opendatasoft portals require no credentials. An API key is only needed for private datasets.

### Configuration

```yaml
plugins:
  opendatasoft:
    enabled: true
    base_url: "https://data.longbeach.gov"    # Portal API base URL
    portal_url: "https://data.longbeach.gov"  # Public portal URL
    city_name: "Long Beach"                   # City/organization name
    timeout: 30.0                             # HTTP timeout (default: 30)
    api_key: "${ODS_API_KEY}"                 # Optional: private datasets only
```

### Tools

- `opendatasoft__search_datasets(query, limit)` - Search the portal catalog (full-text via ODSQL `search()`)
- `opendatasoft__get_dataset(dataset_id)` - Get dataset metadata (title, description, theme, keywords, record count)
- `opendatasoft__get_schema(dataset_id)` - Get field names, types and descriptions for ODSQL clauses
- `opendatasoft__query_data(dataset_id, where, select, order_by, limit)` - Query records with ODSQL (limit capped at 100)
- `opendatasoft__aggregate_data(dataset_id, metrics, group_by, where, order_by, limit)` - Aggregate records with GROUP BY
- `opendatasoft__list_categories()` - List portal themes with dataset counts

### ODSQL notes

The Explore API takes ODSQL fragments rather than full SQL statements:

- String literals use double quotes: `status = "Open"`. Single quotes also work.
- Full-text matching uses `search("text")`; wildcards use `like`, e.g. `name like "North*"`.
- `select` supports fields and aggregates — `count(*)`, `count(field)`, `count(distinct field)`, `sum()`, `avg()`, `min()`, `max()` — each with an `as alias`.
- `group_by` takes a comma-separated list of field names.
- `order_by` takes `field ASC|DESC`, and may reference a `select` alias.
- The records endpoint returns at most 100 rows per call.

Clauses are validated before dispatch: forbidden SQL keywords are rejected outside of quoted literals (keywords inside literals are treated as data), and `aggregate_data` whitelists group-by fields, metric aliases and aggregate expressions.

### Examples

**Search datasets:**
```
Search for datasets about police calls in Long Beach
```

**Get dataset:**
```
Get details about the police-calls-for-service dataset
```

**Get schema (call before query_data):**
```
Get schema for dataset police-calls-for-service
```

**Query data:**
```
Query police-calls-for-service where call_type = "Noise", ordered by received DESC
```

**Aggregate data:**
```
Count police calls by call_type in Long Beach
```

**List categories:**
```
List all dataset themes on Long Beach's open data portal
```

### Opendatasoft API

This plugin uses the Explore API v2.1 (`{base_url}/api/explore/v2.1`):
- `/catalog/datasets` - Catalog list/search
- `/catalog/datasets/{dataset_id}` - Dataset metadata including fields
- `/catalog/datasets/{dataset_id}/records` - Record queries and aggregations
- `/catalog/facets?facet=theme` - Portal-wide themes with counts

See [Opendatasoft Explore API documentation](https://help.opendatasoft.com/apis/ods-explore-v2/) for details.

## Custom Plugins

If your portal doesn't use CKAN, you can create a custom plugin. See [Custom Plugins Guide](CUSTOM_PLUGINS.md) for instructions.

## Examples

See [examples/](../examples/) for complete configuration examples.
