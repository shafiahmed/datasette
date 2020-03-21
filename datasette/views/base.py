import asyncio
import csv
import itertools
import json
import re
import time
import urllib

import jinja2
import pint

from datasette import __version__
from datasette.plugins import pm
from datasette.utils import (
    QueryInterrupted,
    InvalidSql,
    LimitedWriter,
    is_url,
    path_with_added_args,
    path_with_removed_args,
    path_with_format,
    resolve_table_and_format,
    sqlite3,
    to_css_class,
)
from datasette.utils.asgi import (
    AsgiStream,
    AsgiWriter,
    AsgiRouter,
    AsgiView,
    NotFound,
    Response,
)

ureg = pint.UnitRegistry()

HASH_LENGTH = 7


class DatasetteError(Exception):
    def __init__(
        self,
        message,
        title=None,
        error_dict=None,
        status=500,
        template=None,
        messagge_is_html=False,
    ):
        self.message = message
        self.title = title
        self.error_dict = error_dict or {}
        self.status = status
        self.messagge_is_html = messagge_is_html


class BaseView(AsgiView):
    ds = None

    async def head(self, *args, **kwargs):
        response = await self.get(*args, **kwargs)
        response.body = b""
        return response

    def database_url(self, database):
        db = self.ds.databases[database]
        if self.ds.config("hash_urls") and db.hash:
            return "/{}-{}".format(database, db.hash[:HASH_LENGTH])
        else:
            return "/{}".format(database)

    def database_color(self, database):
        return "ff0000"

    async def render(self, templates, request, context):
        template = self.ds.jinja_env.select_template(templates)
        template_context = {
            **context,
            **{
                "database_url": self.database_url,
                "database_color": self.database_color,
            },
        }
        if (
            request
            and request.args.get("_context")
            and self.ds.config("template_debug")
        ):
            return Response.html(
                "<pre>{}</pre>".format(
                    jinja2.escape(json.dumps(template_context, default=repr, indent=4))
                )
            )
        return Response.html(
            await self.ds.render_template(template, template_context, request=request)
        )


class DataView(BaseView):
    name = ""
    re_named_parameter = re.compile(":([a-zA-Z0-9_]+)")

    def __init__(self, datasette):
        self.ds = datasette

    def options(self, request, *args, **kwargs):
        r = Response.text("ok")
        if self.ds.cors:
            r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    def redirect(self, request, path, forward_querystring=True, remove_args=None):
        if request.query_string and "?" not in path and forward_querystring:
            path = "{}?{}".format(path, request.query_string)
        if remove_args:
            path = path_with_removed_args(request, remove_args, path=path)
        r = Response.redirect(path)
        r.headers["Link"] = "<{}>; rel=preload".format(path)
        if self.ds.cors:
            r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    async def data(self, request, database, hash, **kwargs):
        raise NotImplementedError

    async def resolve_db_name(self, request, db_name, **kwargs):
        hash = None
        name = None
        if db_name not in self.ds.databases and "-" in db_name:
            # No matching DB found, maybe it's a name-hash?
            name_bit, hash_bit = db_name.rsplit("-", 1)
            if name_bit not in self.ds.databases:
                raise NotFound("Database not found: {}".format(name))
            else:
                name = name_bit
                hash = hash_bit
        else:
            name = db_name
        name = urllib.parse.unquote_plus(name)
        try:
            db = self.ds.databases[name]
        except KeyError:
            raise NotFound("Database not found: {}".format(name))

        # Verify the hash
        expected = "000"
        if db.hash is not None:
            expected = db.hash[:HASH_LENGTH]
        correct_hash_provided = expected == hash

        if not correct_hash_provided:
            if "table_and_format" in kwargs:

                async def async_table_exists(t):
                    return await db.table_exists(t)

                table, _format = await resolve_table_and_format(
                    table_and_format=urllib.parse.unquote_plus(
                        kwargs["table_and_format"]
                    ),
                    table_exists=async_table_exists,
                    allowed_formats=self.ds.renderers.keys(),
                )
                kwargs["table"] = table
                if _format:
                    kwargs["as_format"] = ".{}".format(_format)
            elif kwargs.get("table"):
                kwargs["table"] = urllib.parse.unquote_plus(kwargs["table"])

            should_redirect = "/{}-{}".format(name, expected)
            if kwargs.get("table"):
                should_redirect += "/" + urllib.parse.quote_plus(kwargs["table"])
            if kwargs.get("pk_path"):
                should_redirect += "/" + kwargs["pk_path"]
            if kwargs.get("as_format"):
                should_redirect += kwargs["as_format"]
            if kwargs.get("as_db"):
                should_redirect += kwargs["as_db"]

            if (
                (self.ds.config("hash_urls") or "_hash" in request.args)
                and
                # Redirect only if database is immutable
                not self.ds.databases[name].is_mutable
            ):
                return name, expected, correct_hash_provided, should_redirect

        return name, expected, correct_hash_provided, None

    def get_templates(self, database, table=None):
        assert NotImplemented

    async def get(self, request, db_name, **kwargs):
        (
            database,
            hash,
            correct_hash_provided,
            should_redirect,
        ) = await self.resolve_db_name(request, db_name, **kwargs)
        if should_redirect:
            return self.redirect(request, should_redirect, remove_args={"_hash"})

        return await self.view_get(
            request, database, hash, correct_hash_provided, **kwargs
        )

    async def as_csv(self, request, database, hash, **kwargs):
        stream = request.args.get("_stream")
        if stream:
            # Some quick sanity checks
            if not self.ds.config("allow_csv_stream"):
                raise DatasetteError("CSV streaming is disabled", status=400)
            if request.args.get("_next"):
                raise DatasetteError("_next not allowed for CSV streaming", status=400)
            kwargs["_size"] = "max"
        # Fetch the first page
        try:
            response_or_template_contexts = await self.data(
                request, database, hash, **kwargs
            )
            if isinstance(response_or_template_contexts, Response):
                return response_or_template_contexts
            else:
                data, _, _ = response_or_template_contexts
        except (sqlite3.OperationalError, InvalidSql) as e:
            raise DatasetteError(str(e), title="Invalid SQL", status=400)

        except (sqlite3.OperationalError) as e:
            raise DatasetteError(str(e))

        except DatasetteError:
            raise

        # Convert rows and columns to CSV
        headings = data["columns"]
        # if there are expanded_columns we need to add additional headings
        expanded_columns = set(data.get("expanded_columns") or [])
        if expanded_columns:
            headings = []
            for column in data["columns"]:
                headings.append(column)
                if column in expanded_columns:
                    headings.append("{}_label".format(column))

        async def stream_fn(r):
            nonlocal data
            writer = csv.writer(LimitedWriter(r, self.ds.config("max_csv_mb")))
            first = True
            next = None
            while first or (next and stream):
                try:
                    if next:
                        kwargs["_next"] = next
                    if not first:
                        data, _, _ = await self.data(request, database, hash, **kwargs)
                    if first:
                        await writer.writerow(headings)
                        first = False
                    next = data.get("next")
                    for row in data["rows"]:
                        if not expanded_columns:
                            # Simple path
                            await writer.writerow(row)
                        else:
                            # Look for {"value": "label": } dicts and expand
                            new_row = []
                            for heading, cell in zip(data["columns"], row):
                                if heading in expanded_columns:
                                    if cell is None:
                                        new_row.extend(("", ""))
                                    else:
                                        assert isinstance(cell, dict)
                                        new_row.append(cell["value"])
                                        new_row.append(cell["label"])
                                else:
                                    new_row.append(cell)
                            await writer.writerow(new_row)
                except Exception as e:
                    print("caught this", e)
                    await r.write(str(e))
                    return

        content_type = "text/plain; charset=utf-8"
        headers = {}
        if self.ds.cors:
            headers["Access-Control-Allow-Origin"] = "*"
        if request.args.get("_dl", None):
            content_type = "text/csv; charset=utf-8"
            disposition = 'attachment; filename="{}.csv"'.format(
                kwargs.get("table", database)
            )
            headers["Content-Disposition"] = disposition

        return AsgiStream(stream_fn, headers=headers, content_type=content_type)

    async def get_format(self, request, database, args):
        """ Determine the format of the response from the request, from URL
            parameters or from a file extension.

            `args` is a dict of the path components parsed from the URL by the router.
        """
        # If ?_format= is provided, use that as the format
        _format = request.args.get("_format", None)
        if not _format:
            _format = (args.pop("as_format", None) or "").lstrip(".")
        else:
            args.pop("as_format", None)
        if "table_and_format" in args:
            db = self.ds.databases[database]

            async def async_table_exists(t):
                return await db.table_exists(t)

            table, _ext_format = await resolve_table_and_format(
                table_and_format=urllib.parse.unquote_plus(args["table_and_format"]),
                table_exists=async_table_exists,
                allowed_formats=self.ds.renderers.keys(),
            )
            _format = _format or _ext_format
            args["table"] = table
            del args["table_and_format"]
        elif "table" in args:
            args["table"] = urllib.parse.unquote_plus(args["table"])
        return _format, args

    async def view_get(self, request, database, hash, correct_hash_provided, **kwargs):
        _format, kwargs = await self.get_format(request, database, kwargs)

        if _format == "csv":
            return await self.as_csv(request, database, hash, **kwargs)

        if _format is None:
            # HTML views default to expanding all foreign key labels
            kwargs["default_labels"] = True

        extra_template_data = {}
        start = time.time()
        status_code = 200
        templates = []
        try:
            response_or_template_contexts = await self.data(
                request, database, hash, **kwargs
            )
            if isinstance(response_or_template_contexts, Response):
                return response_or_template_contexts

            else:
                data, extra_template_data, templates = response_or_template_contexts
        except QueryInterrupted:
            raise DatasetteError(
                """
                SQL query took too long. The time limit is controlled by the
                <a href="https://datasette.readthedocs.io/en/stable/config.html#sql-time-limit-ms">sql_time_limit_ms</a>
                configuration option.
            """,
                title="SQL Interrupted",
                status=400,
                messagge_is_html=True,
            )
        except (sqlite3.OperationalError, InvalidSql) as e:
            raise DatasetteError(str(e), title="Invalid SQL", status=400)

        except (sqlite3.OperationalError) as e:
            raise DatasetteError(str(e))

        except DatasetteError:
            raise

        end = time.time()
        data["query_ms"] = (end - start) * 1000
        for key in ("source", "source_url", "license", "license_url"):
            value = self.ds.metadata(key)
            if value:
                data[key] = value

        # Special case for .jsono extension - redirect to _shape=objects
        if _format == "jsono":
            return self.redirect(
                request,
                path_with_added_args(
                    request,
                    {"_shape": "objects"},
                    path=request.path.rsplit(".jsono", 1)[0] + ".json",
                ),
                forward_querystring=False,
            )

        if _format in self.ds.renderers.keys():
            # Dispatch request to the correct output format renderer
            # (CSV is not handled here due to streaming)
            result = self.ds.renderers[_format](request.args, data, self.name)
            if result is None:
                raise NotFound("No data")

            r = Response(
                body=result.get("body"),
                status=result.get("status_code", 200),
                content_type=result.get("content_type", "text/plain"),
            )
        else:
            extras = {}
            if callable(extra_template_data):
                extras = extra_template_data()
                if asyncio.iscoroutine(extras):
                    extras = await extras
            else:
                extras = extra_template_data
            url_labels_extra = {}
            if data.get("expandable_columns"):
                url_labels_extra = {"_labels": "on"}

            renderers = {
                key: path_with_format(request, key, {**url_labels_extra})
                for key in self.ds.renderers.keys()
            }
            url_csv_args = {"_size": "max", **url_labels_extra}
            url_csv = path_with_format(request, "csv", url_csv_args)
            url_csv_path = url_csv.split("?")[0]
            context = {
                **data,
                **extras,
                **{
                    "renderers": renderers,
                    "url_csv": url_csv,
                    "url_csv_path": url_csv_path,
                    "url_csv_hidden_args": [
                        (key, value)
                        for key, value in urllib.parse.parse_qsl(request.query_string)
                        if key not in ("_labels", "_facet", "_size")
                    ]
                    + [("_size", "max")],
                    "datasette_version": __version__,
                    "config": self.ds.config_dict(),
                },
            }
            if "metadata" not in context:
                context["metadata"] = self.ds.metadata
            r = await self.render(templates, request=request, context=context)
            r.status = status_code

        ttl = request.args.get("_ttl", None)
        if ttl is None or not ttl.isdigit():
            if correct_hash_provided:
                ttl = self.ds.config("default_cache_ttl_hashed")
            else:
                ttl = self.ds.config("default_cache_ttl")

        return self.set_response_headers(r, ttl)

    def set_response_headers(self, response, ttl):
        # Set far-future cache expiry
        if self.ds.cache_headers and response.status == 200:
            ttl = int(ttl)
            if ttl == 0:
                ttl_header = "no-cache"
            else:
                ttl_header = "max-age={}".format(ttl)
            response.headers["Cache-Control"] = ttl_header
        response.headers["Referrer-Policy"] = "no-referrer"
        if self.ds.cors:
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    async def custom_sql(
        self,
        request,
        database,
        hash,
        sql,
        editable=True,
        canned_query=None,
        metadata=None,
        _size=None,
        named_parameters=None,
        write=False,
    ):
        params = request.raw_args
        if "sql" in params:
            params.pop("sql")
        if "_shape" in params:
            params.pop("_shape")

        # Extract any :named parameters
        named_parameters = named_parameters or self.re_named_parameter.findall(sql)
        named_parameter_values = {
            named_parameter: params.get(named_parameter) or ""
            for named_parameter in named_parameters
        }

        # Set to blank string if missing from params
        for named_parameter in named_parameters:
            if named_parameter not in params:
                params[named_parameter] = ""

        extra_args = {}
        if params.get("_timelimit"):
            extra_args["custom_time_limit"] = int(params["_timelimit"])
        if _size:
            extra_args["page_size"] = _size

        templates = ["query-{}.html".format(to_css_class(database)), "query.html"]
        if canned_query:
            templates.insert(
                0,
                "query-{}-{}.html".format(
                    to_css_class(database), to_css_class(canned_query)
                ),
            )

        if write:
            if request.method == "POST":
                params = await request.post_vars()
                write_ok = await self.ds.databases[database].execute_write(
                    sql, params, block=True
                )
                return self.redirect(request, request.path)
            else:

                async def extra_template():
                    return {
                        "request": request,
                        "path_with_added_args": path_with_added_args,
                        "path_with_removed_args": path_with_removed_args,
                        "named_parameter_values": named_parameter_values,
                    }

                return (
                    {
                        "database": database,
                        "rows": [],
                        "truncated": False,
                        "columns": [],
                        "query": {"sql": sql, "params": params},
                    },
                    extra_template,
                    templates,
                )

        else:
            results = await self.ds.execute(
                database, sql, params, truncate=True, **extra_args
            )
            columns = [r[0] for r in results.description]

        async def extra_template():
            display_rows = []
            for row in results.rows:
                display_row = []
                for column, value in zip(results.columns, row):
                    display_value = value
                    # Let the plugins have a go
                    # pylint: disable=no-member
                    plugin_value = pm.hook.render_cell(
                        value=value,
                        column=column,
                        table=None,
                        database=database,
                        datasette=self.ds,
                    )
                    if plugin_value is not None:
                        display_value = plugin_value
                    else:
                        if value in ("", None):
                            display_value = jinja2.Markup("&nbsp;")
                        elif is_url(str(display_value).strip()):
                            display_value = jinja2.Markup(
                                '<a href="{url}">{url}</a>'.format(
                                    url=jinja2.escape(value.strip())
                                )
                            )
                    display_row.append(display_value)
                display_rows.append(display_row)
            return {
                "display_rows": display_rows,
                "custom_sql": True,
                "named_parameter_values": named_parameter_values,
                "editable": editable,
                "canned_query": canned_query,
                "metadata": metadata,
                "config": self.ds.config_dict(),
                "request": request,
                "path_with_added_args": path_with_added_args,
                "path_with_removed_args": path_with_removed_args,
                "hide_sql": "_hide_sql" in params,
            }

        return (
            {
                "database": database,
                "rows": results.rows,
                "truncated": results.truncated,
                "columns": columns,
                "query": {"sql": sql, "params": params},
            },
            extra_template,
            templates,
        )
