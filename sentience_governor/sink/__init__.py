"""Sink Writer — stdout, file, and local HTTP sinks with fail-open semantics."""

from sentience_governor.sink.writer import FileSink, HttpLocalSink, SinkWriter, StdoutSink

__all__ = ["SinkWriter", "StdoutSink", "FileSink", "HttpLocalSink"]
