# Built-in Plugins Reference

OpenContext includes built-in plugins for CKAN, Socrata, and ArcGIS Hub open data portals.

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
- `ckan__execute_sql(sql)` - Execute a validated `SELECT` query against the datastore
- `ckan__aggregate_data(resource_id, metrics, group_by, filters, having, order_by, limit)` - GROUP BY aggregations without writing SQL

**`aggregate_data` notes:**
- `metrics` maps alias to expression, e.g. `{"cnt": "count(*)", "avg_amt": "avg(amount)"}`. Supported: `count(*)`, `count(field)`, `count(distinct field)`, `sum()`, `avg()`, `min()`, `max()`, `stddev()`, `variance()`
- `having` keys are aggregate expressions or declared metric aliases; string values may carry a comparison operator (`{"count(*)": ">= 5"}`), bare numbers default to `>`
- `order_by` accepts `"field"`, `"-field"` (descending), or `"field ASC|DESC"`
- All identifiers and expressions are validated against safe whitelists before SQL is built

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

## ArcGIS Plugin

For ArcGIS Hub / ArcGIS Open Data portals (e.g., hub.arcgis.com, city Hub sites).

### Configuration

```yaml
plugins:
  arcgis:
    enabled: true
    portal_url: "https://hub.arcgis.com"   # ArcGIS Hub portal URL
    city_name: "Your City"
    timeout: 120
    # token: "${ARCGIS_TOKEN}"             # Optional: Bearer token for private items
    # trusted_service_hosts:               # Extra hosts for self-hosted Feature Services
    #   - "gis.yourcity.gov"
```

### Tools

- `arcgis__search_datasets(query, limit)` - Search the Hub catalog (query required)
- `arcgis__get_dataset(dataset_id)` - Get Hub item metadata
- `arcgis__get_aggregations(field, query)` - Aggregate counts for a field
- `arcgis__get_schema(dataset_id)` - Get Feature Service layer schema
- `arcgis__query_data(dataset_id, where, out_fields, limit)` - Query records (limit max 1000)

### Security notes

Feature Service URLs resolved from Hub metadata are restricted to `*.arcgis.com`, the configured portal host, or hosts listed in `trusted_service_hosts` (exact host or subdomain) — an SSRF guard. Add self-hosted city GIS domains to `trusted_service_hosts` when Hub datasets reference them. `where` clauses are validated with a forbidden-keyword scan that skips quoted string literals, so values like `status = 'SET'` are fine.

### ArcGIS API

This plugin uses a two-hop flow: the Hub API resolves a dataset ID to its Feature Service URL, then records are queried from that service.

## Custom Plugins

If your portal doesn't use CKAN, you can create a custom plugin. See [Custom Plugins Guide](CUSTOM_PLUGINS.md) for instructions.

## Examples

See [examples/](../examples/) for complete configuration examples.
