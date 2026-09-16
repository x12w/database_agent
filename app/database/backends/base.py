"""Abstract database backend — defines the interface for all database types."""

from abc import ABC, abstractmethod
from contextlib import contextmanager


class DatabaseBackend(ABC):
    """Interface that all database backends must implement."""

    @abstractmethod
    def connect(self, host: str, port: int, user: str, password: str, database: str, **kwargs):
        """Establish a connection. Returns the raw connection object."""
        ...

    @abstractmethod
    def ping(self, conn) -> bool:
        """Check if connection is alive."""
        ...

    @abstractmethod
    def close(self, conn):
        """Close the connection."""
        ...

    @abstractmethod
    def create_cursor(self, conn):
        """Create a dict-like cursor from the connection."""
        ...

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """Quote a table/column identifier for use in SQL."""
        ...

    @abstractmethod
    def placeholder(self) -> str:
        """Return the parameter placeholder character (%s for most)."""
        ...
