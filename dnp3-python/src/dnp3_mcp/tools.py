"""Compatibility imports; implementation lives in domain, application and protocol modules."""

import inspect as inspect
import json as json
import logging as logging
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Annotated as Annotated
from typing import Any as Any

import anyio as anyio
from pydantic import BeforeValidator as BeforeValidator
from pydantic import Field as Field
from pydantic import StrictBool as StrictBool
from pydantic import StrictInt as StrictInt

from dnp3_mcp.application.resources import ToolResources as ToolResources
from dnp3_mcp.config import MAX_LINK_ADDRESS as MAX_LINK_ADDRESS
from dnp3_mcp.dnp3_master import DNP3Master as DNP3Master
from dnp3_mcp.domain.errors import MAX_POINT_INDEX as MAX_POINT_INDEX
from dnp3_mcp.domain.errors import MAX_READ_COUNT as MAX_READ_COUNT
from dnp3_mcp.domain.errors import DNP3MasterError as DNP3MasterError
from dnp3_mcp.domain.model import NOT_SENT as NOT_SENT
from dnp3_mcp.domain.model import POINT_TYPES as POINT_TYPES
from dnp3_mcp.domain.model import Alias as Alias
from dnp3_mcp.domain.model import Count as Count
from dnp3_mcp.domain.model import EventClass as EventClass
from dnp3_mcp.domain.model import OutputValue as OutputValue
from dnp3_mcp.domain.model import OutstationAddress as OutstationAddress
from dnp3_mcp.domain.model import PointIndex as PointIndex
from dnp3_mcp.domain.model import PointSpec as PointSpec
from dnp3_mcp.domain.model import StartIndex as StartIndex
from dnp3_mcp.domain.model import _int_field as _int_field
from dnp3_mcp.domain.model import _output_value as _output_value
from dnp3_mcp.domain.model import envelope as envelope
from dnp3_mcp.domain.model import err as err
from dnp3_mcp.domain.model import logger as logger
from dnp3_mcp.domain.model import ok as ok
from dnp3_mcp.domain.model import parse_point_spec as parse_point_spec
from dnp3_mcp.infrastructure.maps import PointMap as PointMap

from .protocol.support import CONSUMES_EVENTS as CONSUMES_EVENTS
from .protocol.support import LOCAL_ONLY as LOCAL_ONLY
from .protocol.support import READ_ONLY as READ_ONLY
from .protocol.support import WRITES as WRITES
from .protocol.support import FastMCP as FastMCP
from .protocol.support import ToolAnnotations as ToolAnnotations
from .protocol.support import request_cancel_scope as request_cancel_scope
from .protocol.tools import register_tools as register_tools
