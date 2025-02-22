# -*- coding: utf-8 -*-
#
# Copyright (C) 2021-2024 CERN.
# Copyright (C)      2024 University of Münster.
#
# Invenio-Vocabularies is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see LICENSE file for more
# details.

"""Readers module."""

import csv
import gzip
import json
import re
import tarfile
import zipfile
from abc import ABC, abstractmethod
from json.decoder import JSONDecodeError

import requests
import yaml
from lxml import etree
from lxml.html import parse as html_parse

from .errors import ReaderError
from .xml import etree_to_dict

try:
    import oaipmh_scythe
except ImportError:
    oaipmh_scythe = None


class BaseReader(ABC):
    """Base reader."""

    def __init__(self, origin=None, mode="r", *args, **kwargs):
        """Constructor.

        :param origin: Data source (e.g. filepath).
                       Can be none in case of piped readers.
        """
        self._origin = origin
        self._mode = mode

    @abstractmethod
    def _iter(self, fp, *args, **kwargs):
        """Yields data objects file pointer."""
        pass

    def read(self, item=None, *args, **kwargs):
        """Reads from item or opens the file descriptor from origin."""
        if item:
            yield from self._iter(fp=item, *args, **kwargs)
        else:
            with open(self._origin, self._mode) as file:
                yield from self._iter(fp=file, *args, **kwargs)


class YamlReader(BaseReader):
    """Yaml reader."""

    def _iter(self, fp, *args, **kwargs):
        """Reads a yaml file and returns a dictionary per element."""
        data = yaml.safe_load(fp) or []
        for entry in data:
            yield entry


class TarReader(BaseReader):
    """Tar reader."""

    def __init__(self, *args, mode="r|gz", regex=None, **kwargs):
        """Constructor."""
        self._regex = re.compile(regex) if regex else None
        super().__init__(*args, mode=mode, **kwargs)

    def _iter(self, fp, *args, **kwargs):
        """Iterates through the files in the archive."""
        for member in fp:
            match = not self._regex or self._regex.search(member.name)
            if member.isfile() and match:
                yield fp.extractfile(member)

    def read(self, item=None, *args, **kwargs):
        """Opens a tar archive or uses the given file pointer."""
        if item:
            if isinstance(item, tarfile.TarFile):
                yield from self._iter(fp=item, *args, **kwargs)
            else:
                # If the item is not already a TarFile (e.g. if it is a BytesIO), try to create a TarFile from the item.
                with tarfile.open(mode=self._mode, fileobj=item) as archive:
                    yield from self._iter(fp=archive, *args, **kwargs)
        else:
            with tarfile.open(self._origin, self._mode) as archive:
                yield from self._iter(fp=archive, *args, **kwargs)


class SimpleHTTPReader(BaseReader):
    """Simple HTTP Reader."""

    def __init__(self, origin, id=None, ids=None, content_type=None, *args, **kwargs):
        """Constructor."""
        assert id or ids
        self._ids = ids if ids else [id]
        self.content_type = content_type
        super().__init__(origin, *args, **kwargs)

    def _iter(self, url, *args, **kwargs):
        """Queries an URL."""
        base_url = url
        headers = {"Accept": self.content_type}

        for id_ in self._ids:
            url = base_url.format(id=id_)
            resp = requests.get(url, headers=headers)
            if resp.status_code != 200:
                # todo add logging/fail
                pass

            yield resp.content

    def read(self, item=None, *args, **kwargs):
        """Chooses between item and origin as url."""
        url = item if item else self._origin
        yield from self._iter(url=url, *args, **kwargs)


class ZipReader(BaseReader):
    """ZIP reader."""

    def __init__(self, *args, options=None, regex=None, **kwargs):
        """Constructor."""
        self._options = options or {}
        self._regex = re.compile(regex) if regex else None
        super().__init__(*args, **kwargs)

    def _iter(self, fp, *args, **kwargs):
        """Iterates through the files in the archive."""
        for member in fp.infolist():
            match = not self._regex or self._regex.search(member.filename)
            if not member.is_dir() and match:
                yield fp.open(member)

    def read(self, item=None, *args, **kwargs):
        """Opens a Zip archive or uses the given file pointer."""
        # https://docs.python.org/3/library/zipfile.html
        if item:
            if isinstance(item, zipfile.ZipFile):
                yield from self._iter(fp=item, *args, **kwargs)
            else:
                # If the item is not already a ZipFile (e.g. if it is a BytesIO), try to create a ZipFile from the item.
                with zipfile.ZipFile(item, **self._options) as archive:
                    yield from self._iter(fp=archive, *args, **kwargs)
        else:
            with zipfile.ZipFile(self._origin, **self._options) as archive:
                yield from self._iter(fp=archive, *args, **kwargs)


class JsonReader(BaseReader):
    """JSON object reader."""

    def _iter(self, fp, *args, **kwargs):
        """Reads (loads) a json object and yields its items."""
        try:
            entries = json.load(fp)
            if isinstance(entries, list):
                for entry in entries:
                    yield entry
            else:
                yield entries  # just one entry
        except JSONDecodeError as err:
            raise ReaderError(f"Cannot decode JSON file {fp.name}: {str(err)}")


class JsonLinesReader(BaseReader):
    """JSON Lines reader."""

    def _iter(self, fp, *args, **kwargs):
        for idx, line in enumerate(fp):
            try:
                data = json.loads(line)
                if isinstance(data, list):
                    for entry in data:
                        yield entry
                else:
                    yield data  # just one entry
            except JSONDecodeError as err:
                raise ReaderError(
                    f"Cannot decode JSON line {fp.name}:{idx}: {str(err)}"
                )


class GzipReader(BaseReader):
    """Gzip reader."""

    def _iter(self, fp, *args, **kwargs):
        with gzip.open(fp) as gp:
            yield gp


class CSVReader(BaseReader):
    """Reads a CSV file and returns a dictionary per element."""

    def __init__(self, *args, csv_options=None, as_dict=True, **kwargs):
        """Constructor."""
        self.csv_options = csv_options or {}
        self.as_dict = as_dict
        super().__init__(*args, **kwargs)

    def _iter(self, fp, *args, **kwargs):
        """Reads a csv file and returns a dictionary per element."""
        csvfile = fp
        if self.as_dict:
            reader = csv.DictReader(csvfile, **self.csv_options)
        else:
            reader = csv.reader(csvfile, **self.csv_options)
        for row in reader:
            yield row


class XMLReader(BaseReader):
    """XML reader."""

    def _iter(self, fp, *args, **kwargs):
        """Read and parse an XML file to dict."""
        # NOTE: We parse HTML, to skip XML validation and strip XML namespaces
        xml_tree = html_parse(fp).getroot()
        record = etree_to_dict(xml_tree)["html"]["body"].get("record")

        if not record:
            raise ReaderError(f"Record not found in XML entry.")

        yield record


class OAIPMHReader(BaseReader):
    """OAIPMH reader."""

    def __init__(
        self,
        *args,
        base_url=None,
        metadata_prefix=None,
        set=None,
        from_date=None,
        until_date=None,
        verb=None,
        **kwargs,
    ):
        """Constructor."""
        self._base_url = base_url
        self._metadata_prefix = metadata_prefix if not None else "oai_dc"
        self._set = set
        self._until = until_date
        self._from = from_date
        self._verb = verb if not None else "ListRecords"
        super().__init__(*args, **kwargs)

    def _iter(self, scythe, *args, **kwargs):
        """Read and parse an OAIPMH stream to dict."""

        class OAIRecord(oaipmh_scythe.models.Record):
            """An XML unpacking implementation for more complicated formats."""

            def get_metadata(self):
                """Extract and return the record's metadata as a dictionary."""
                return xml_to_dict(
                    self.xml.find(f".//{self._oai_namespace}metadata").getchildren()[0],
                )

        if self._verb == "ListRecords":
            scythe.class_mapping["ListRecords"] = OAIRecord
            try:
                records = scythe.list_records(
                    from_=self._from,
                    until=self._until,
                    metadata_prefix=self._metadata_prefix,
                    set_=self._set,
                    ignore_deleted=True,
                )
                for record in records:
                    yield {"record": record}
            except oaipmh_scythe.NoRecordsMatch:
                raise ReaderError("No records found in OAI-PMH request.")
        else:
            scythe.class_mapping["GetRecord"] = OAIRecord
            try:
                headers = scythe.list_identifiers(
                    from_=self._from,
                    until=self._until,
                    metadata_prefix=self._metadata_prefix,
                    set_=self._set,
                    ignore_deleted=True,
                )
                for header in headers:
                    record = scythe.get_record(
                        identifier=header.identifier,
                        metadata_prefix=self._metadata_prefix,
                    )
                    yield {"record": record}
            except oaipmh_scythe.NoRecordsMatch:
                raise ReaderError("No records found in OAI-PMH request.")

    def read(self, item=None, *args, **kwargs):
        """Reads from item or opens the file descriptor from origin."""
        if item:
            raise NotImplementedError(
                "OAIPMHReader does not support being chained after another reader"
            )
        else:
            with oaipmh_scythe.Scythe(self._base_url) as scythe:
                yield from self._iter(scythe=scythe, *args, **kwargs)


def xml_to_dict(tree: etree._Element):
    """Convert an XML tree to a dictionary.

    This function takes an XML element tree and converts it into a dictionary.

    Args:
        tree: The root element of the XML tree to be converted.

    Returns:
        A dictionary with the key "record".
    """
    dict_obj = dict()
    dict_obj["record"] = etree.tostring(tree)

    return dict_obj
