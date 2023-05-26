from concurrent.futures import ALL_COMPLETED, FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import copy
from time import monotonic
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from tawazi._dag.digraph import DiGraphEx
from tawazi._errors import ErrorStrategy, TawaziTimeoutError
from tawazi.consts import Identifier
from tawazi.node.node import ExecNode


def _xn_active_in_call(xn: ExecNode, xns_dict: Dict[Identifier, ExecNode]) -> bool:
    if isinstance(xn.active, bool):
        return xn.active
    return bool(xns_dict[xn.active.id].result)


def copy_non_setup_xns(x_nodes: Dict[str, ExecNode]) -> Dict[str, ExecNode]:
    """Deep copy all ExecNodes except setup ExecNodes because they are shared throughout the DAG instance.

    Args:
        x_nodes: Dict[str, ExecNode] x_nodes to be deep copied

    Returns:
        Dict[str, ExecNode] copy of x_nodes
    """
    # TODO: separate setup xnodes and non setup xndoes.
    #  maybe use copy instead of deepcopy for the non setup xnodes!? I think this is a bad idea it won't work
    x_nodes_copy = {}
    for id_, x_nd in x_nodes.items():
        # if execnode is a setup node, it shouldn't be copied
        if x_nd.setup:
            x_nodes_copy[id_] = x_nd
        else:
            # no need to deepcopy. we only need to know if self.result is NoVal or not (TODO: fix this COmment)
            x_nodes_copy[id_] = copy(x_nd)
    return x_nodes_copy


#####################
# scheduler Functions
#####################


def get_num_running_threads(_futures: Dict[Identifier, "Future[Any]"]) -> int:
    # use not future.done() because there is no guarantee that Thread pool will directly execute
    # the submitted thread
    return sum([not future.done() for future in _futures.values()])


def get_highest_priority_nodes(nodes: List[ExecNode]) -> List[ExecNode]:
    highest_priority = max(node.priority for node in nodes)
    return [node for node in nodes if node.priority == highest_priority]


def get_next_timeout(
    futures_launch_time: Dict[Identifier, float],
    futures: Dict[Identifier, "Future[Any]"],
    xns_dict: Dict[Identifier, ExecNode],
) -> Optional[float]:
    """Get the timeout to wait before an ExecNode issuance date arrives.

    Args:
        futures_launch_time (Dict[Identifier, float]): The launch time of the futures (submission time).
        futures (Dict[Identifier, &quot;Future[Any]&quot;]): The futures that are submitted (running, scheduled & done).
        xns_dict (Dict[Identifier, ExecNode]): The ExecNodes whose Futures are submitted.

    Returns:
        Optional[float]: The next timeout to use in wait.
    """
    # start next_timeout as infinity.
    # 1st timeout encountered will reduce it to a finite number.
    next_timeout = float("inf")
    for id_, fut in futures.items():
        # if future is not done, then it should be running (This is not guaranteed in ThreadPoolExecutor)
        if not fut.done():
            launch_time = futures_launch_time[id_]
            timeout = xns_dict[id_].timeout
            logger.debug(
                f"next_timeout: {next_timeout}, launch_time: {launch_time}, timeout: {timeout}"
            )

            if timeout is not None:
                next_timeout = min(next_timeout, launch_time + timeout)

    if next_timeout == float("inf"):
        return None
    return next_timeout - monotonic()


def raise_timeout_error_conditionally(
    done_: Set["Future[Any]"], running: Set["Future[Any]"], next_timeout: Optional[float]
) -> None:
    # done_ doesn't contain any future. => timeout reached
    if not done_:
        raise TawaziTimeoutError(
            f"Timeout reached while waiting for {running} to finish. Waited a total of {next_timeout} seconds"
        )


################
# The scheduler!
################
def execute(
    *,
    node_dict: Dict[Identifier, ExecNode],
    max_concurrency: int,
    behavior: ErrorStrategy,
    graph: DiGraphEx,
    modified_node_dict: Optional[Dict[str, ExecNode]] = None,
    call_id: str = "",
) -> Dict[Identifier, Any]:
    """Thread safe execution of the DAG.

    (Except for the setup nodes! Please run DAG.setup() in a single thread because its results will be cached).

    Args:
        node_dict: dictionary identifying ExecNodes.
        max_concurrency: maximum number of threads to be used for the execution.
        behavior: the behavior to be used in case of error.
        graph: the graph ids to be executed
        modified_node_dict: A dictionary of the ExecNodes that have been modified by setting the input parameters of the DAG.
        call_id (str): A unique identifier for the execution.
            It can be used to distinguish the id of the call inside the thread.
            It might be useful to debug and to exchange information between the main thread and the sub-threads (per-node threads)

    Returns:
        node_dict: dictionary with keys the name of the function and value the result after the execution
    """
    # 0.1 deepcopy the node_dict in order to modify the results inside every node and make the dag reusable
    #     modified_node_dict are used to modify the values inside the ExecNode corresponding
    #     to the input arguments provided to the whole DAG (ArgExecNode)
    xns_dict = modified_node_dict or copy_non_setup_xns(node_dict)

    # 0.2 prune the graph from the ArgExecNodes so that they don't get executed in the ThreadPool
    precomputed_xns_ids = [id_ for id_ in graph if xns_dict[id_].executed]
    for id_ in precomputed_xns_ids:
        graph.remove_node(id_)

    # 0.3 create variables related to futures
    futures: Dict[Identifier, "Future[Any]"] = {}
    done: Set["Future[Any]"] = set()
    running: Set["Future[Any]"] = set()
    futures_launch_time: Dict[Identifier, float] = {}

    # 0.4 get the candidates root nodes that can be executed
    # runnable_nodes_ids will be empty if all root nodes are running
    runnable_xns_ids = graph.root_nodes()

    with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix=call_id) as executor:
        while len(graph):
            # Attempt to run **A SINGLE** root node.

            # 6. block scheduler execution if no root node can be executed.
            #    this can occur in two cases:
            #       1. if maximum concurrency is reached
            #       2. if no runnable node exists (i.e. all root nodes are being executed)
            #    in both cases: block until a node finishes
            #       => a new root node will be available
            num_running_threads = get_num_running_threads(futures)
            num_runnable_nodes_ids = len(runnable_xns_ids)
            if num_running_threads == max_concurrency or num_runnable_nodes_ids == 0:
                # must wait and not submit any workers before a worker ends
                # (that might create a new more prioritized node) to be executed
                logger.debug(f"Waiting for ExecNodes {running} to finish. Finished running {done}")

                # next_timeout is used to determine the timeout of the next node to be executed
                next_timeout = get_next_timeout(futures_launch_time, futures, xns_dict)

                done_, running = wait(running, return_when=FIRST_COMPLETED, timeout=next_timeout)

                raise_timeout_error_conditionally(done_, running, next_timeout)

                done = done.union(done_)

            # TODO: Optimize this part! I can check the finished nodes by looking at done_ instead of checking all futures
            # 1. among the finished futures:
            #       1. checks for exceptions
            #       2. and remove them from the graph
            for id_, fut in futures.items():
                if fut.done() and id_ in graph:
                    logger.debug(f"Remove ExecNode {id_} from the graph")
                    handle_exception(behavior, graph, fut, id_)
                    graph.remove_node(id_)

            # 2. list the root nodes that aren't being executed
            runnable_xns_ids = list(set(graph.root_nodes()) - set(futures.keys()))

            # 3. if no runnable node exist, go to step 6 (wait for a node to finish)
            #   (This **might** create a new root node)
            if len(runnable_xns_ids) == 0:
                logger.debug("No runnable Nodes available")
                continue

            # 4. choose a node to run
            # 4.1 get the most prioritized node to run
            # 4.1.1 get all the nodes that have the highest priority
            runnable_xns = [xns_dict[node_id] for node_id in runnable_xns_ids]
            highest_priority_xns = get_highest_priority_nodes(runnable_xns)

            # 4.1.2 get the node with the highest compound priority
            # (randomly selected if multiple are suggested)
            highest_priority_xns.sort(key=lambda node: node.compound_priority)
            xn = highest_priority_xns[-1]

            logger.info(f"{xn.id} will run!")

            # 4.2 if the current node must be run sequentially, wait for a running node to finish.
            # in that case we must prune the graph to re-check whether a new root node
            # (maybe with a higher priority) has been created => continue the loop
            # Note: This step might run a number of times in the while loop
            #       before the exec_node gets submitted
            num_running_threads = get_num_running_threads(futures)
            if xn.is_sequential and num_running_threads != 0:
                logger.debug(
                    f"{xn.id} must not run in parallel. Wait for the end of a node in {running}"
                )
                # next_timeout is used to determine the timeout of the next node to be executed
                next_timeout = get_next_timeout(futures_launch_time, futures, xns_dict)

                done_, running = wait(running, return_when=FIRST_COMPLETED, timeout=next_timeout)

                raise_timeout_error_conditionally(done_, running, next_timeout)

                # go to step 6
                continue

            # 5.1 dynamic graph pruning
            if not _xn_active_in_call(xn, xns_dict):
                logger.debug(f"Prune {xn.id} from the graph")
                graph.remove_recursively(xn.id)
                continue

            # 5.2 submit the exec node to the executor
            exec_future = executor.submit(xn._execute, node_dict=xns_dict)
            running.add(exec_future)
            futures[xn.id] = exec_future
            futures_launch_time[xn.id] = monotonic()

            # 5.3 wait for the sequential node to finish
            # This code is executed only if this node is being executed purely by itself
            if xn.is_sequential:
                logger.debug(f"Wait for all Futures to finish because {xn.id} is sequential.")

                # next_timeout is used to determine the timeout of the next node to be executed
                next_timeout = get_next_timeout(futures_launch_time, futures, xns_dict)

                # ALL_COMPLETED is equivalent to FIRST_COMPLETED because there is only a single future running!
                done_, running = wait(
                    futures.values(), return_when=ALL_COMPLETED, timeout=next_timeout
                )

                raise_timeout_error_conditionally(done_, running, next_timeout)

    return xns_dict


def handle_exception(
    behavior: ErrorStrategy, graph: DiGraphEx, fut: "Future[Any]", id_: Identifier
) -> None:
    """Checks if futures have produced exceptions, and handles them according to the specified behavior.

    Args:
        behavior: the behavior to adopt in case of exception
        graph: the graph
        fut: the thread future
        id_: the Identifier of the current ExecNode

    Raises:
        NotImplementedError: if self.behavior is not known
    """
    if behavior == ErrorStrategy.strict:
        # will raise the first encountered exception if there's one
        # no simpler way to check for exception, and not supported by flake8
        _res = fut.result()  # noqa: F841

    else:
        try:
            _res = fut.result()  # noqa: F841

        except Exception as e:
            logger.exception(f"The feature {id_} encountered the following error:")

            if behavior == ErrorStrategy.permissive:
                logger.warning("Ignoring exception as the behavior is set to permissive")

            elif behavior == ErrorStrategy.all_children:
                # remove all its children. Current node will be removed directly afterwards
                successors = list(graph.successors(id_))
                for children_ids in successors:
                    # TODO: implement a test for all_children! it should fail!
                    # Afterwards include parameter to remove the node itself or not
                    graph.remove_recursively(children_ids)

            else:
                raise NotImplementedError(f"Unknown behavior name: {behavior}") from e
