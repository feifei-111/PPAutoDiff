import contextlib
from collections import namedtuple
from .actions import get_action
from paddle.fluid.layers.utils import flatten, to_sequence, map_structure, pack_sequence_as
from .utils import for_each_grad_tensor, for_each_tensor, clone_tensors, TableView, TreeView
import warnings
from .stack_info import print_frames

class Counter: 
    def __init__(self):
        self.clear()

    def clear(self):
        self.id = 0

    def get_id(self):
        ret = self.id
        self.id += 1
        return ret


class ReportItem: 
    def __init__(self, type, step, input, output, net, net_id, frame_info, frames):
        assert type in ['forward', 'backward'], "type can only be one of ['forward', 'backward']"
        self.type = type
        self.step = step
        """
        self.input is a tuple: (tensor, ...)
        """
        self.input = clone_tensors(input)
        self.output = clone_tensors(output)
        self.net = net
        self.net_id = net_id
        self.fwd_item = None
        self.bwd_item = None
        self.frame_info = frame_info
        self.frames = frames
        self.input_grads = self._gen_input_grads()

    def set_forward(self, fwd):
        assert self.type == "backward", "can't set forward for non-backward item."
        fwd.bwd_item = self
        self.fwd_item = fwd

    def _gen_input_grads(self):
        if self.type == "forward": 
            return None
        assert input is not None, "Backward while input is None, not expected."
        
        return [None for i in for_each_grad_tensor(self.input)]

    def set_input_grads(self, nth, value):
        assert nth < len(self.input_grads)
        self.input_grads[nth] = value

    def print_stacks(self):
        print_frames(self.frames)

    def stacks(self):
        return self.frames

    def compare_tensors(self): 
        if self.type == "forward": 
            return for_each_tensor(self.output)
        if self.type == "backward": 
            return for_each_tensor(self.input_grads)

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        strings = []
        strings.append("ReportItem: \n    type={}".format(self.type))
        strings.append("    step_idx: {}".format(self.step))
        return "\n".join(strings)

        
class Report:
    def __init__(self, name):
        self.name = name
        self.items = []

    def put_item(self, type, input, output, net, net_id, frame_info, frames): 
        step = global_counter.get_id()
        self.items.append(ReportItem(
            type=type,
            step=step, 
            input=input, 
            output=output,
            net=net,
            net_id=net_id,
            frame_info=frame_info,
            frames=frames,
        ))
        return self.items[-1]

    def get_fwd_items(self):
        sorted(self.items, key=lambda x: x.step)
        return list(filter(lambda x: x.type=='forward', self.items))

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        sorted(self.items, key=lambda x: x.step)
        strings = []
        strings.append("Report name is: " + self.name)
        for item in self.items:
            strings.append("    " + str(item.step) + ": [{}]".format(type(item.net)))
        return "\n".join(strings)

global_report = None
global_counter = Counter()

@contextlib.contextmanager
def report_guard(report):
    global global_report
    old = global_report
    try:
        global_report = report
        global_counter.clear()
        yield
    finally:
        global_report = old


def current_report():
    if global_report is None: 
        raise RuntimeError("Please call `current_report()` within contextmanager `report_guard(Report())`.")
    return global_report 


def print_info(paddle_item, torch_item, exc, step_idx, grad=False): 
    print ("FAILED !!!")
    if grad: 
        print ("    Diff found in `Backward Stagy` in step: {}, net_id is {} vs {}".format(step_idx, paddle_item.net_id, torch_item.net_id))
    else:   
        print ("    Diff found in `Forward  Stagy` in step: {}, net_id is {} vs {}".format(step_idx, paddle_item.net_id, torch_item.net_id))
    print ("    Type of layer is  : {} vs {}".format(type(torch_item.net), type(paddle_item.net)))
    print (str(exc))

    print ("\n\nPaddle Stacks:" )
    print ("=========================")
    paddle_item.print_stacks()
    print ("Torch  Stacks:" )
    print ("=========================")
    torch_item.print_stacks()


def check_forward_and_backward(torch_rep, paddle_rep, cfg):
    """
    TODO(@xiongkun): 
    More abundant printing methods can be supported later，For example, interactive printing mode，Tree Printing mode，Currently, only list printing is supported.
    """
    torch_fwd_items = torch_rep.get_fwd_items()
    paddle_fwd_items = paddle_rep.get_fwd_items()
    torch_fwd_items = TableView(torch_fwd_items, lambda x: x.net_id)
    paddle_tree_view = TreeView(paddle_fwd_items)
    assert len(torch_fwd_items) == len(paddle_fwd_items), "Difference length of torch_fwd_items and paddel_items, make sure the paddle layer and torch module have the same valid sublayer."

    backward_items = []
    # forward check
    for idx, paddle_item in enumerate(paddle_tree_view.traversal_forward()):
        assert paddle_item.net_id in torch_fwd_items, "Torch has no corresponding module for {}".format(type(paddle_item.net))
        torch_item = torch_fwd_items[paddle_item.net_id]
        assert torch_item.type == paddle_item.type and paddle_item.type == "forward"
        act = get_action(torch_item.net, paddle_item.net)
        try: 
            backward_items.append([ torch_item.bwd_item, paddle_item.bwd_item ])
            act(torch_item, paddle_item, cfg)
        except Exception as e: 
            print_info(paddle_item, torch_item, e, idx, grad=False)
            return False

    print ("forward {} steps compared.".format(len(paddle_fwd_items)))

    # backward check
    # backward_map map from id(paddle_backward_item) to torch_backward_item
    backward_map = TableView(backward_items, lambda x: id(x[1]))
    """
    TODO(xiongkun): the order is problematic because we consider the tree structure as a chain structure.
          so, always the root layer is calculated first. but we want the first layer with diff.
          
    """
    for idx, paddle_item in enumerate(paddle_tree_view.traversal_backward()): 
        torch_item, paddle_item = backward_map[id(paddle_item.bwd_item)]
        assert torch_item.type == paddle_item.type and paddle_item.type == "backward"
        act = get_action(torch_item.net, paddle_item.net)
        try: 
            act(torch_item, paddle_item, cfg)
        except Exception as e: 
            print_info(paddle_item, torch_item, e, idx, grad=True)
            return False

    print ("bacward {} steps compared.".format(len(backward_items)))

    # total status
    print ("SUCCESS !!!")
    return True
    
