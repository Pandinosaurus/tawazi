from tawazi import dag, xn
global_var = 0
@xn
def add(x, y):
    global global_var
    global_var += 1
    return x + y

@dag
def pipe():
    a = add(1, 2)
    b = add(a, 3)
    c = add(a, 4)
    d = add(b, c)
    return d

def test_pipe() -> None:
    assert pipe() == 13
    assert global_var == 4