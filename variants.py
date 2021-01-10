# -*- coding: utf-8 -*-


class SimpleBoard:
    def __init__(self, initial_fen=None, chess960=False):
        self.initial_fen = initial_fen
        self.move_stack = []
        self.color = True if initial_fen.split()[1] == "w" else False
        self.chess960 = chess960

    def push(self, move):
        self.move_stack.append(move)
        self.color = not self.color

    def pop(self):
        del self.move_stack[-1]
        self.color = not self.color

    def is_game_over(self):
        return False

    def fen(self):
        if self.initial_fen is None:
            return self.starting_fen
        else:
            return self.initial_fen


class StandardBoard(SimpleBoard):
    uci_variant = "chess"
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class CrazyhouseBoard(SimpleBoard):
    uci_variant = "crazyhouse"
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR[] w KQkq - 0 1"

class ShogiBoard(SimpleBoard):
    uci_variant = "shogi"
    starting_fen = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL[] b - 1"

VARIANT2BOARD = {
    "chess": StandardBoard,
    "crazyhouse": CrazyhouseBoard,
    "shogi": ShogiBoard
}
