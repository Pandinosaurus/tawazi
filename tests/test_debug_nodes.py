# type: ignore
import pytest

from tawazi import dag, xn
from tawazi.errors import TawaziBaseException


@xn
def stub(img):
    print(f"did operation on {img}")
    return img


@xn(debug=True)
def my_len(img):
    len_img = len(img)
    pytest.my_len_has_ran = True
    return len_img


@xn(debug=True)
def is_positive_len(len_img):
    # this node depends of the my_len!
    if len_img > 0:
        print("positive")
    else:
        print("negative")

    pytest.is_positive_len_has_ran = True


def test_pipeline_with_debug_node():

    pytest.my_len_has_ran = False
    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = True

    @dag
    def pipeline(img):
        img = stub(img)
        len_ = my_len(img)
        return img

    assert [1, 2, 3] == pipeline([1, 2, 3])
    assert pytest.my_len_has_ran == True


def test_pipeline_without_debug_node():

    pytest.my_len_has_ran = False
    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = False

    @dag
    def pipeline(img):
        img = stub(img)
        len_ = my_len(img)
        return img

    assert [1, 2, 3] == pipeline([1, 2, 3])
    assert pytest.my_len_has_ran == False


test_pipeline_without_debug_node()


def test_interdependant_debug_nodes():

    pytest.my_len_has_ran = False
    pytest.is_positive_len_has_ran = False
    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = True

    @dag
    def pipeline(img):
        img = stub(img)
        len_ = my_len(img)
        is_positive_len(len_)
        return img

    assert [1, 2, 3] == pipeline([1, 2, 3])
    assert pytest.my_len_has_ran == True
    assert pytest.is_positive_len_has_ran == True


def test_wrongly_defined_pipeline():
    with pytest.raises(TawaziBaseException):

        @dag
        def pipeline(img):
            len_ = my_len(img)
            # wrongly defined dependency node!
            # a production node depends on a debug node!
            return stub(len_)


@xn(debug=True)
def print_(in1):
    pytest.prin_share_var = in1


@xn(debug=True)
def incr(in1):
    res = in1 + 1
    pytest.inc_shared_var = res
    return res


@dag
def triple_incr_debug(in1):
    return incr(incr(incr(in1, twz_tag="1st")))


def test_triple_incr_debug():

    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = True

    assert triple_incr_debug(1) == 4


def test_triple_incr_no_debug():

    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = False

    assert triple_incr_debug(1) == None


def test_triple_incr_debug_subgraph():

    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = True

    # by only running a single dependency all subsequent debug nodes shall run
    assert triple_incr_debug(1, twz_nodes=["1st"]) == 4


def test_reachable_debuggable_node_in_subgraph():

    import tawazi

    tawazi.Cfg.RUN_DEBUG_NODES = True

    @dag
    def pipe(in1):
        res1 = stub(in1)
        res2 = incr(res1)
        print_(res2)
        return res1

    assert pipe(2, twz_nodes=["stub"]) == 2
    assert pytest.prin_share_var == 3

    pytest.prin_share_var == None

    assert pipe(0, twz_nodes=["stub", "incr"]) == 0
    assert pytest.prin_share_var == 1


# should this be True ???
# def test_return_debug_node_value_should_raise_error():
# pass
