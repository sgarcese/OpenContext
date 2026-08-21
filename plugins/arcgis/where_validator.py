"""WHERE clause validator for ArcGIS Feature Service queries.

Provides light sanitization of SQL WHERE clauses to prevent
injection of destructive operations. Subclasses
:class:`BaseQueryValidator` to reuse the shared forbidden-keyword
scan (which includes GRANT/REVOKE/DECLARE/SET that this plugin
previously lacked) while keeping the ArcGIS-specific public API
``validate(where) -> str``.
"""

from core.query_validator import BaseQueryValidator


class WhereValidator(BaseQueryValidator):
    """Validates WHERE clause strings for Feature Service queries.

    Unlike :meth:`BaseQueryValidator.validate_query`, the ArcGIS Feature
    Service ``where`` parameter is a WHERE-clause fragment (not a full
    SELECT statement), so the prefix and dangerous-pattern checks do not
    apply. Only the forbidden-keyword scan is reused.
    """

    @classmethod
    def validate(cls, where: str) -> str:
        """Validate and sanitize a WHERE clause string.

        Args:
            where: SQL WHERE clause string

        Returns:
            The original WHERE clause if valid, or ``"1=1"`` if empty/None

        Raises:
            ValueError: If the clause contains forbidden SQL keywords
        """
        if not where:
            return "1=1"

        where = where.strip()
        if not where:
            return "1=1"

        forbidden = cls.scan_forbidden_keywords(where)
        if forbidden:
            raise ValueError(f"Forbidden keyword detected in WHERE clause: {forbidden}")

        return where