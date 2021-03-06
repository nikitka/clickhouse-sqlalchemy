from sqlalchemy import schema, types as sqltypes, exc, util as sa_util
from sqlalchemy.engine import default, reflection
from sqlalchemy.sql import (
    compiler, expression, type_api, literal_column, elements
)
from sqlalchemy.types import DATE, DATETIME, FLOAT
from sqlalchemy.util import inspect_getargspec, warn

from .. import types
from ..util import compat


# Column spec
colspecs = {}


# Type converters
ischema_names = {
    'Int64': types.Int64,
    'Int32': types.Int32,
    'Int16': types.Int16,
    'Int8': types.Int8,
    'UInt64': types.UInt64,
    'UInt32': types.UInt32,
    'UInt16': types.UInt16,
    'UInt8': types.UInt8,
    'Date': DATE,
    'DateTime': DATETIME,
    'Float64': FLOAT,
    'Float32': FLOAT,
    'String': types.String,
    'FixedString': types.String,
    'Enum8': types.Enum8,
    'Enum16': types.Enum16,
    '_array': types.Array,
    '_nullable': types.Nullable
}


class ClickHouseIdentifierPreparer(compiler.IdentifierPreparer):
    def _escape_identifier(self, value):
        value = value.replace(self.escape_quote, self.escape_to_quote)
        return value.replace('%', '%%')


class ClickHouseCompiler(compiler.SQLCompiler):
    def visit_mod_binary(self, binary, operator, **kw):
        return self.process(binary.left, **kw) + " %% " + \
            self.process(binary.right, **kw)

    def post_process_text(self, text):
        return text.replace('%', '%%')

    def visit_count_func(self, fn, **kw):
        # count accepts zero arguments.
        return 'count%s' % self.process(fn.clause_expr, **kw)

    def visit_case(self, clause, **kwargs):
        text = 'CASE '
        if clause.value is not None:
            text += clause.value._compiler_dispatch(self, **kwargs) + " "
        for cond, result in clause.whens:
            text += 'WHEN ' + cond._compiler_dispatch(
                self, **kwargs
            ) + ' THEN ' + result._compiler_dispatch(
                self, **kwargs) + " "
        if clause.else_ is None:
            raise exc.CompileError('ELSE clause is required in CASE')

        text += 'ELSE ' + clause.else_._compiler_dispatch(
            self, **kwargs
        ) + ' END'
        return text

    def visit_if__func(self, func, **kw):
        return "(%s) ? (%s) : (%s)" % (
            self.process(func.clauses.clauses[0], **kw),
            self.process(func.clauses.clauses[1], **kw),
            self.process(func.clauses.clauses[2], **kw)
        )

    def visit_column(self, column, include_table=True, **kwargs):
        # Columns prefixed with table name are not supported
        return super(ClickHouseCompiler, self).visit_column(
            column, include_table=False, **kwargs
        )

    def limit_clause(self, select, **kw):
        text = ''
        if select._limit_clause is not None:
            text += ' \n LIMIT '
            if select._offset_clause is not None:
                text += self.process(select._offset_clause, **kw) + ', '
            text += self.process(select._limit_clause, **kw)
        else:
            if select._offset_clause is not None:
                raise exc.CompileError('OFFSET without LIMIT is not supported')

        return text

    def visit_lambda(self, lambda_, **kw):
        func = lambda_.func
        spec = inspect_getargspec(func)

        if spec.varargs:
            raise exc.CompileError('Lambdas with *args are not supported')

        if spec.keywords:
            raise exc.CompileError('Lambdas with **kwargs are not supported')

        text = ', '.join(spec.args) + ' -> '

        args = [literal_column(arg) for arg in spec.args]
        text += self.process(func(*args), **kw)

        return text

    def visit_extract(self, extract, **kw):
        field = self.extract_map.get(extract.field, extract.field)
        column = self.process(extract.expr, **kw)
        if field == 'year':
            return 'toYear(%s)' % column
        elif field == 'month':
            return 'toMonth(%s)' % column
        elif field == 'day':
            return 'toDayOfMonth(%s)' % column
        else:
            return column

    def visit_join(self, join, asfrom=False, **kwargs):
        join_type = " "

        if join.global_:
            join_type += "GLOBAL "

        if join.any:
            join_type += "ANY "

        if join.all:
            join_type += "ALL "

        if join.isouter:
            join_type += "LEFT OUTER JOIN "
        else:
            join_type += "INNER JOIN "

        if not isinstance(join.onclause, elements.Tuple):
            raise exc.CompileError(
                "Only tuple elements are supported. "
                "Got: %s" % type(join.onclause)
            )

        return (
            join.left._compiler_dispatch(self, asfrom=True, **kwargs) +
            join_type +
            join.right._compiler_dispatch(self, asfrom=True, **kwargs) +
            " USING " + join.onclause._compiler_dispatch(self, **kwargs)
        )

    def _compose_select_body(
            self, text, select, inner_columns, froms, byfrom, kwargs):
        text += ', '.join(inner_columns)

        if froms:
            text += " \nFROM "

            if select._hints:
                text += ', '.join(
                    [f._compiler_dispatch(self, asfrom=True,
                                          fromhints=byfrom, **kwargs)
                     for f in froms])
            else:
                text += ', '.join(
                    [f._compiler_dispatch(self, asfrom=True, **kwargs)
                     for f in froms])
        else:
            text += self.default_from()

        sample_clause = getattr(select, '_sample_clause', None)

        if sample_clause is not None:
            text += self.sample_clause(select, **kwargs)

        if select._whereclause is not None:
            t = select._whereclause._compiler_dispatch(self, **kwargs)
            if t:
                text += " \nWHERE " + t

        if select._group_by_clause.clauses:
            text += self.group_by_clause(select, **kwargs)

        if select._having is not None:
            t = select._having._compiler_dispatch(self, **kwargs)
            if t:
                text += " \nHAVING " + t

        if select._order_by_clause.clauses:
            text += self.order_by_clause(select, **kwargs)

        if (select._limit_clause is not None or
                select._offset_clause is not None):
            text += self.limit_clause(select, **kwargs)

        if select._for_update_arg is not None:
            text += self.for_update_clause(select, **kwargs)

        return text

    def sample_clause(self, select, **kw):
        return " \nSAMPLE " + self.process(select._sample_clause, **kw)

    def group_by_clause(self, select, **kw):
        text = ""

        group_by = select._group_by_clause._compiler_dispatch(
            self, **kw)

        if group_by:
            text = " GROUP BY " + group_by

            if getattr(select, '_with_totals', False):
                text += " WITH TOTALS"

        return text


class ClickHouseDDLCompiler(compiler.DDLCompiler):
    def visit_create_column(self, create, **kw):
        column = create.element
        nullable = column.nullable

        # All columns including synthetic PKs must be 'nullable'
        column.nullable = True

        rv = super(ClickHouseDDLCompiler, self).visit_create_column(
            create, **kw
        )
        column.nullable = nullable

        return rv

    def visit_primary_key_constraint(self, constraint):
        # Do not render PKs.
        return ''

    def visit_engine(self, engine):
        compiler = self.sql_compiler

        def compile_param(expr):
            if not isinstance(expr, expression.ColumnClause):
                if not hasattr(expr, 'self_group'):
                    # assuming base type (int, string, etc.)
                    return compat.text_type(expr)
                else:
                    expr = expr.self_group()
            return compiler.process(
                expr, include_table=False, literal_binds=True
            )

        engine_params = engine.get_params()
        text = engine.name()
        if not engine_params:
            return text

        text += '('

        compiled_params = []
        for param in engine_params:
            if isinstance(param, tuple):
                compiled = (
                    '(' +
                    ', '.join(compile_param(p) for p in param) +
                    ')'
                )
            else:
                compiled = compile_param(param)

            compiled_params.append(compiled)

        text += ', '.join(compiled_params)

        return text + ')'

    def create_table_suffix(self, table):
        cluster = table.dialect_options['clickhouse']['cluster']
        if cluster:
            return 'ON CLUSTER ' + self.preparer.quote(cluster)

        return ''

    def post_create_table(self, table):
        engine = getattr(table, 'engine', None)

        if not engine:
            raise exc.CompileError("No engine for table '%s'" % table.name)

        return ' ENGINE = ' + self.process(engine)

    def visit_drop_table(self, drop):
        text = '\nDROP TABLE '

        if drop.if_exists:
            text += 'IF EXISTS '

        rv = text + self.preparer.format_table(drop.element)

        if drop.on_cluster:
            rv += (
                ' ON CLUSTER ' +
                self.preparer.quote(drop.on_cluster)
            )

        return rv


class ClickHouseTypeCompiler(compiler.GenericTypeCompiler):
    def visit_string(self, type_, **kw):
        if type_.length is None:
            return 'String'
        else:
            return 'FixedString(%s)' % type_.length

    def visit_array(self, type_, **kw):
        item_type = type_api.to_instance(type_.item_type)
        return "Array(%s)" % self.process(item_type, **kw)

    def visit_nullable(self, type_, **kw):
        nested_type = type_api.to_instance(type_.nested_type)
        return "Nullable(%s)" % self.process(nested_type, **kw)

    def visit_int8(self, type_, **kw):
        return 'Int8'

    def visit_uint8(self, type_, **kw):
        return 'UInt8'

    def visit_int16(self, type_, **kw):
        return 'Int16'

    def visit_uint16(self, type_, **kw):
        return 'UInt16'

    def visit_int32(self, type_, **kw):
        return 'Int32'

    def visit_uint32(self, type_, **kw):
        return 'UInt32'

    def visit_int64(self, type_, **kw):
        return 'Int64'

    def visit_uint64(self, type_, **kw):
        return 'UInt64'

    def visit_date(self, type_, **kw):
        return 'Date'

    def visit_datetime(self, type_, **kw):
        return 'DateTime'

    def visit_float32(self, type_, **kw):
        return 'Float32'

    def visit_float64(self, type_, **kw):
        return 'Float64'

    def visit_numeric(self, type_, **kw):
        return 'Decimal(%s,%s)' % (type_.precision, type_.scale)

    def _render_enum(self, db_type, type_, **kw):
        choices = (
            "'%s' = %d" %
            (x.name.replace("'", "\\'"), x.value) for x in type_.enum_type
        )
        return "%s(%s)" % (db_type, ', '.join(choices))

    def visit_enum8(self, type_, **kw):
        return self._render_enum('Enum8', type_, **kw)

    def visit_enum16(self, type_, **kw):
        return self._render_enum('Enum16', type_, **kw)


class ClickHouseExecutionContextBase(default.DefaultExecutionContext):
    @sa_util.memoized_property
    def should_autocommit(self):
        return False  # No DML supported, never autocommit


class ClickHouseDialect(default.DefaultDialect):
    name = 'clickhouse'
    supports_cast = True
    supports_unicode_statements = True
    supports_unicode_binds = True
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False
    supports_native_decimal = True
    supports_native_boolean = False
    supports_alter = True
    supports_sequences = False
    supports_native_enum = True  # Do not render check constraints on enums.
    supports_multivalues_insert = True

    max_identifier_length = 127
    default_paramstyle = 'pyformat'
    colspecs = colspecs
    ischema_names = ischema_names
    convert_unicode = True
    returns_unicode_strings = True
    description_encoding = None
    postfetch_lastrowid = False

    preparer = ClickHouseIdentifierPreparer
    type_compiler = ClickHouseTypeCompiler
    statement_compiler = ClickHouseCompiler
    ddl_compiler = ClickHouseDDLCompiler

    construct_arguments = [
        (schema.Table, {
            "data": [],
            "cluster": None
        })
    ]

    def _execute(self, connection, sql):
        raise NotImplementedError

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        return self.get_table_names(connection, schema, **kw)

    def has_table(self, connection, table_name, schema=None):
        query = 'EXISTS TABLE {}'.format(table_name)
        for r in self._execute(connection, query):
            if r.result == 1:
                return True
        return False

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        query = 'DESCRIBE TABLE {}'.format(table_name)
        rows = self._execute(connection, query)

        columns = []
        for name, format_type, default_type, default_expression in rows:
            columns.append(self._get_column_info(name, format_type))
        return columns

    def _get_col_type(self, name, spec):
        if spec.startswith('Array'):
            inner = spec[6:-1]
            coltype = self.ischema_names['_array']
            return coltype(self._get_col_type(name, inner))

        elif spec.startswith('FixedString'):
            length = int(spec[12:-1])
            return self.ischema_names['FixedString'](length)

        elif spec.startswith('Nullable'):
            inner = spec[9:-1]
            coltype = self.ischema_names['_nullable']
            return coltype(self._get_col_type(name, inner))

        else:
            try:
                return self.ischema_names[spec]
            except KeyError:
                warn("Did not recognize type '%s' of column '%s'" %
                     (spec, name))
                return sqltypes.NullType

    def _get_column_info(self, name, format_type):
        return {
            'name': name,
            'type': self._get_col_type(name, format_type),
            'nullable': True,
            'default': None,
        }

    @reflection.cache
    def get_schema_names(self, connection, **kw):
        return [row.name for row in connection.execute('SHOW DATABASES')]

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        # No support for foreign keys.
        return []

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        # No support for primary keys.
        return []

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kw):
        # No support for indexes.
        return []

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        return [row.name for row in self._execute(connection, 'SHOW TABLES')]

    def do_rollback(self, dbapi_connection):
        # No support for transactions.
        pass

    def do_executemany(self, cursor, statement, parameters, context=None):
        cursor.executemany(statement, parameters, context=context)

    def do_execute(self, cursor, statement, parameters, context=None):
        cursor.execute(statement, parameters, context=context)

    def _check_unicode_returns(self, connection, additional_tests=None):
        return True

    def _check_unicode_description(self, connection):
        return True
