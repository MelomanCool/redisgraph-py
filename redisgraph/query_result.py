from .node import Node
from .edge import Edge
from .path import Path
from .exceptions import VersionMismatchException
from prettytable import PrettyTable
from redis import ResponseError
from collections import OrderedDict

LABELS_ADDED = 'Labels added'
NODES_CREATED = 'Nodes created'
NODES_DELETED = 'Nodes deleted'
RELATIONSHIPS_DELETED = 'Relationships deleted'
PROPERTIES_SET = 'Properties set'
RELATIONSHIPS_CREATED = 'Relationships created'
INDICES_CREATED = "Indices created"
INDICES_DELETED = "Indices deleted"
CACHED_EXECUTION = "Cached execution"
INTERNAL_EXECUTION_TIME = 'internal execution time'

STATS = [LABELS_ADDED, NODES_CREATED, PROPERTIES_SET, RELATIONSHIPS_CREATED,
         NODES_DELETED, RELATIONSHIPS_DELETED, INDICES_CREATED, INDICES_DELETED,
         CACHED_EXECUTION, INTERNAL_EXECUTION_TIME]


class ResultSetColumnTypes:
    COLUMN_UNKNOWN = 0
    COLUMN_SCALAR = 1
    COLUMN_NODE = 2       # Unused as of RedisGraph v2.1.0, retained for backwards compatibility.
    COLUMN_RELATION = 3   # Unused as of RedisGraph v2.1.0, retained for backwards compatibility.


class ResultSetScalarTypes:
    VALUE_UNKNOWN = 0
    VALUE_NULL = 1
    VALUE_STRING = 2
    VALUE_INTEGER = 3
    VALUE_BOOLEAN = 4
    VALUE_DOUBLE = 5
    VALUE_ARRAY = 6
    VALUE_EDGE = 7
    VALUE_NODE = 8
    VALUE_PATH = 9
    VALUE_MAP = 10


class QueryResult:

    def __init__(self, graph, response):
        self.graph = graph
        self.header = []
        self.result_set = []

        # incase of an error an exception will be raised
        self._check_for_errors(response)

        if len(response) == 1:
            self.parse_statistics(response[0])
        else:
            # start by parsing statistics, matches the one we have
            self.parse_statistics(response[2])  # Third element, after header and records.
            self.parse_results(response)
            if len(response) == 4:
                self.parse_metadata(response[3])

    def _check_for_errors(self, response):
        if isinstance(response[0], ResponseError):
            error = response[0]
            if str(error) == "version mismatch":
                version = response[1]
                error = VersionMismatchException(version)
            raise error

        # If we encountered a run-time error, the last response element will be an exception.
        if isinstance(response[-1], ResponseError):
            raise response[-1]

    def parse_results(self, raw_result_set):
        self.header = self.parse_header(raw_result_set)

        # Empty header.
        if len(self.header) == 0:
            return

        self.result_set = self.parse_records(raw_result_set)

    def parse_statistics(self, raw_statistics):
        self.statistics = {}

        # decode statistics
        for idx, stat in enumerate(raw_statistics):
            if isinstance(stat, bytes):
                raw_statistics[idx] = stat.decode()

        for s in STATS:
            v = self._get_value(s, raw_statistics)
            if v is not None:
                self.statistics[s] = v

    def parse_metadata(self, raw_metadata):
        # Decode metadata:
        # [
        #   ["version", VERSION],
        #   ["labels", [[VALUE_STRING, "label_1"] ... ]],
        #   ["relationship types ", [[VALUE_STRING, "reltype_1"] ... ]],
        #   ["property keys", [[VALUE_STRING, "prop_1"] ... ]]
        # ]
        version = raw_metadata[0][1]
        raw_labels = raw_metadata[1][1]
        raw_reltypes = raw_metadata[2][1]
        raw_props = raw_metadata[3][1]

        # Arrays to be passed into the internal graph structure.
        labels = [None] * len(raw_labels)
        reltypes = [None] * len(raw_reltypes)
        properties = [None] * len(raw_props)

        for idx, label in enumerate(raw_labels):
            labels[idx] = self.parse_scalar(label)

        for idx, reltype in enumerate(raw_reltypes):
            reltypes[idx] = self.parse_scalar(reltype)

        for idx, prop in enumerate(raw_props):
            properties[idx] = self.parse_scalar(prop)

        # Update the graph's internal metadata.
        self.graph.refresh_metadata(version, labels, reltypes, properties)

    def parse_header(self, raw_result_set):
        # An array of column name/column type pairs.
        header = raw_result_set[0]
        return header

    def parse_records(self, raw_result_set):
        records = []
        result_set = raw_result_set[1]
        for row in result_set:
            record = []
            for idx, cell in enumerate(row):
                if self.header[idx][0] == ResultSetColumnTypes.COLUMN_SCALAR:
                    record.append(self.parse_scalar(cell))
                elif self.header[idx][0] == ResultSetColumnTypes.COLUMN_NODE:
                    record.append(self.parse_node(cell))
                elif self.header[idx][0] == ResultSetColumnTypes.COLUMN_RELATION:
                    record.append(self.parse_edge(cell))
                else:
                    print("Unknown column type.\n")
            records.append(record)

        return records

    def parse_entity_properties(self, props):
        # [[name, value type, value] X N]
        properties = {}
        for prop in props:
            prop_name = self.graph.get_property(prop[0])
            prop_value = self.parse_scalar(prop[1:])
            properties[prop_name] = prop_value

        return properties

    def parse_string(self, cell):
        if isinstance(cell, bytes):
            return cell.decode()
        elif not isinstance(cell, str):
            return str(cell)
        else:
            return cell

    def parse_node(self, cell):
        # Node ID (integer),
        # [label string offset (integer)],
        # [[name, value type, value] X N]

        node_id = int(cell[0])
        label = None
        if len(cell[1]) != 0:
            label = self.graph.get_label(cell[1][0])
        properties = self.parse_entity_properties(cell[2])
        return Node(node_id=node_id, label=label, properties=properties)

    def parse_edge(self, cell):
        # Edge ID (integer),
        # reltype string offset (integer),
        # src node ID offset (integer),
        # dest node ID offset (integer),
        # [[name, value, value type] X N]

        edge_id = int(cell[0])
        relation = self.graph.get_relation(cell[1])
        src_node_id = int(cell[2])
        dest_node_id = int(cell[3])
        properties = self.parse_entity_properties(cell[4])
        return Edge(src_node_id, relation, dest_node_id, edge_id=edge_id, properties=properties)

    def parse_path(self, cell):
        nodes = self.parse_scalar(cell[0])
        edges = self.parse_scalar(cell[1])
        return Path(nodes, edges)

    def parse_map(self, cell):
        m = OrderedDict()
        n_entries = len(cell)

        # A map is an array of key value pairs.
        # 1. key (string)
        # 2. array: (value type, value)
        for i in range(0, n_entries, 2):
            key = self.parse_string(cell[i])
            m[key] = self.parse_scalar(cell[i+1])

        return m

    def parse_scalar(self, cell):
        scalar_type = int(cell[0])
        value = cell[1]
        scalar = None

        if scalar_type == ResultSetScalarTypes.VALUE_NULL:
            scalar = None

        elif scalar_type == ResultSetScalarTypes.VALUE_STRING:
            scalar = self.parse_string(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_INTEGER:
            scalar = int(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_BOOLEAN:
            value = value.decode() if isinstance(value, bytes) else value
            if value == "true":
                scalar = True
            elif value == "false":
                scalar = False
            else:
                print("Unknown boolean type\n")

        elif scalar_type == ResultSetScalarTypes.VALUE_DOUBLE:
            scalar = float(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_ARRAY:
            # array variable is introduced only for readability
            scalar = array = value
            for i in range(len(array)):
                scalar[i] = self.parse_scalar(array[i])

        elif scalar_type == ResultSetScalarTypes.VALUE_NODE:
            scalar = self.parse_node(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_EDGE:
            scalar = self.parse_edge(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_PATH:
            scalar = self.parse_path(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_MAP:
            scalar = self.parse_map(value)

        elif scalar_type == ResultSetScalarTypes.VALUE_UNKNOWN:
            print("Unknown scalar type\n")

        return scalar

    """Prints the data from the query response:
       1. First row result_set contains the columns names. Thus the first row in PrettyTable
          will contain the columns.
       2. The row after that will contain the data returned, or 'No Data returned' if there is none.
       3. Prints the statistics of the query.
    """

    def pretty_print(self):
        if not self.is_empty():
            header = [col[1] for col in self.header]
            tbl = PrettyTable(header)

            for row in self.result_set:
                record = []
                for idx, cell in enumerate(row):
                    if type(cell) is Node:
                        record.append(cell.toString())
                    elif type(cell) is Edge:
                        record.append(cell.toString())
                    else:
                        record.append(cell)
                tbl.add_row(record)

            if len(self.result_set) == 0:
                tbl.add_row(['No data returned.'])

            print(str(tbl) + '\n')

        for stat in self.statistics:
            print("%s %s" % (stat, self.statistics[stat]))

    def is_empty(self):
        return len(self.result_set) == 0

    @staticmethod
    def _get_value(prop, statistics):
        for stat in statistics:
            if prop in stat:
                return float(stat.split(': ')[1].split(' ')[0])

        return None

    def _get_stat(self, stat):
        return self.statistics[stat] if stat in self.statistics else 0

    @property
    def labels_added(self):
        return self._get_stat(LABELS_ADDED)

    @property
    def nodes_created(self):
        return self._get_stat(NODES_CREATED)

    @property
    def nodes_deleted(self):
        return self._get_stat(NODES_DELETED)

    @property
    def properties_set(self):
        return self._get_stat(PROPERTIES_SET)

    @property
    def relationships_created(self):
        return self._get_stat(RELATIONSHIPS_CREATED)

    @property
    def relationships_deleted(self):
        return self._get_stat(RELATIONSHIPS_DELETED)

    @property
    def indices_created(self):
        return self._get_stat(INDICES_CREATED)

    @property
    def indices_deleted(self):
        return self._get_stat(INDICES_DELETED)

    @property
    def cached_execution(self):
        return self._get_stat(CACHED_EXECUTION) == 1

    @property
    def run_time_ms(self):
        return self._get_stat(INTERNAL_EXECUTION_TIME)
