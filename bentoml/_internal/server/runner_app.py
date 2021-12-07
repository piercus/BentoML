import json
import typing as t
import asyncio
import logging
from typing import TYPE_CHECKING
from functools import partial

from simple_di import inject
from simple_di import Provide

from bentoml._internal.runner.utils import Params
from bentoml._internal.runner.utils import PAYLOAD_META_HEADER
from bentoml._internal.runner.utils import multipart_to_payload_params
from bentoml._internal.runner.container import AutoContainer
from bentoml._internal.marshal.dispatcher import CorkDispatcher

from ..server.base_app import BaseAppFactory
from ..configuration.containers import BentoMLContainer

feedback_logger = logging.getLogger("bentoml.feedback")
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from starlette.routing import BaseRoute
    from starlette.requests import Request
    from starlette.responses import Response

    from ..runner import Runner
    from ..tracing import Tracer


class RunnerAppFactory(BaseAppFactory):
    @inject
    def __init__(
        self,
        runner: "Runner",
        instance_id: t.Optional[int] = None,
        tracer: "Tracer" = Provide[BentoMLContainer.tracer],
    ) -> None:
        self.runner = runner
        self.instance_id = instance_id
        self.tracer = tracer

        from starlette.responses import Response

        TooManyRequests = partial(Response, status_code=427)

        options = self.runner.batch_options
        if options.enabled:
            options = self.runner.batch_options
            self.dispatcher = CorkDispatcher(
                max_latency_in_ms=options.max_latency_ms,
                max_batch_size=options.max_batch_size,
                fallback=TooManyRequests,
            )
        else:
            self.dispatcher = None
        self.input_batch_axis = options.input_batch_axis
        self.output_batch_axis = options.output_batch_axis

    @property
    def name(self) -> str:
        return self.runner.name

    @property
    def on_startup(self) -> t.List[t.Callable[[], None]]:
        on_startup = super().on_startup
        on_startup.insert(0, self.runner._impl.setup)  # type: ignore[reportPrivateUsage]
        return on_startup

    @property
    def on_shutdown(self) -> t.List[t.Callable[[], None]]:
        on_shutdown = super().on_shutdown
        if self.dispatcher is not None:
            on_shutdown.insert(0, self.dispatcher.shutdown)
        return on_shutdown

    @property
    def routes(self) -> t.List["BaseRoute"]:
        """
        Setup routes for Runner server, including:

        /healthz        liveness probe endpoint
        /readyz         Readiness probe endpoint
        /metrics        Prometheus metrics endpoint

        /run
        /run_batch
        """
        from starlette.routing import Route

        routes = super().routes
        routes.append(Route("/run_batch", self.async_run_batch, methods=["POST"]))

        if self.dispatcher is not None:
            _func = self.dispatcher(self._async_cork_run)
            routes.append(Route("/run", _func, methods=["POST"]))
        else:
            routes.append(Route("/run", self.async_run, methods=["POST"]))
        return routes

    async def _async_cork_run(
        self, requests: t.Iterable["Request"]
    ) -> t.List["Response"]:
        from starlette.responses import Response

        assert self._is_ready

        params_list = await asyncio.gather(
            *tuple(multipart_to_payload_params(r) for r in requests)
        )
        params_list = [
            params.map(AutoContainer.payload_to_single) for params in params_list
        ]
        params = Params.agg(
            params_list,
            lambda i: AutoContainer.singles_to_batch(
                i, batch_axis=self.input_batch_axis
            ),
        )
        batch_ret = await self.runner.async_run_batch(*params.args, **params.kwargs)
        rets = AutoContainer.batch_to_singles(
            batch_ret, batch_axis=self.output_batch_axis
        )
        payloads = map(AutoContainer.single_to_payload, rets)
        return [
            Response(
                payload.data,
                headers={
                    PAYLOAD_META_HEADER: json.dumps(payload.meta),
                    "Server": f"BentoML-Runner/{self.runner.name}/{self.instance_id}",
                },
            )
            for payload in payloads
        ]

    async def async_run(self, request: "Request") -> "Response":
        from starlette.responses import Response

        assert self._is_ready

        params = await multipart_to_payload_params(request)
        params = params.map(AutoContainer.payload_to_single)
        ret = await self.runner.async_run(*params.args, **params.kwargs)
        payload = AutoContainer.single_to_payload(ret)
        return Response(
            payload.data,
            headers={
                PAYLOAD_META_HEADER: json.dumps(payload.meta),
                "Server": f"BentoML-Runner/{self.runner.name}/{self.instance_id}",
            },
        )

    async def async_run_batch(self, request: "Request") -> "Response":
        from starlette.responses import Response

        assert self._is_ready

        params = await multipart_to_payload_params(request)
        params = params.map(AutoContainer.payload_to_batch)
        ret = await self.runner.async_run_batch(*params.args, **params.kwargs)
        payload = AutoContainer.batch_to_payload(ret)
        return Response(
            payload.data,
            headers={
                PAYLOAD_META_HEADER: json.dumps(payload.meta),
                "Server": f"BentoML-Runner/{self.runner.name}/{self.instance_id}",
            },
        )