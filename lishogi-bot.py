import variants
import argparse
import engine_wrapper
import model
import json
import lishogi
import logging
import multiprocessing
import traceback
import logging_pool
import signal
import sys
import time
import backoff
from config import load_config
from conversation import Conversation, ChatLine
from functools import partial
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError
from urllib3.exceptions import ProtocolError
from ColorLogger import enable_color_logging

logger = logging.getLogger(__name__)

try:
    from http.client import RemoteDisconnected
    # New in version 3.5: Previously, BadStatusLine('') was raised.
except ImportError:
    from http.client import BadStatusLine as RemoteDisconnected

__version__ = "0.1"

terminated = False

def signal_handler(signal, frame):
    global terminated
    logger.debug("Recieved SIGINT. Terminating client.")
    terminated = True

signal.signal(signal.SIGINT, signal_handler)

def is_final(exception):
    return isinstance(exception, HTTPError) and exception.response.status_code < 500

def upgrade_account(li):
    if li.upgrade_to_bot_account() is None:
        return False

    logger.info("Succesfully upgraded to Bot Account!")
    return True

#@backoff.on_exception(backoff.expo, BaseException, max_time=600, giveup=is_final)
#def watch_control_stream(control_queue, li):
#    response = li.get_event_stream()
#    control_queue.put_nowait({"type": "connected"})
#    try:
#        for line in response.iter_lines():
#            if line:
#                event = json.loads(line.decode('utf-8'))
#                control_queue.put_nowait(event)
#            else:
#                control_queue.put_nowait({"type": "ping"})
#    except (RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError) as exception:
#        logger.error("Terminating client due to connection error")
#        traceback.print_exception(type(exception), exception, exception.__traceback__)
#        control_queue.put_nowait({"type": "terminated"})

def watch_control_stream(control_queue, li):
    while True:
        try:
            response = li.get_event_stream()
            control_queue.put_nowait({"type": "connected"})
            for line in response.iter_lines():
                if line:
                    try:
                        event = json.loads(line.decode('utf-8'))
                    except Exception:
                        print("Failed json.loads()", line)
                        raise
                    if event["type"] == "terminated":
                        logger.info("Server sent 'terminated' event. Going to get new event stream...")
                        # try to get new event stream
                        time.sleep(10)
                        break
                    else:
                        control_queue.put_nowait(event)
                else:
                    control_queue.put_nowait({"type": "ping"})
        except Exception:
            logger.exception("ERROR: Exception in whatch_control_stream!")
            time.sleep(10)


def start(li, user_profile, engine_factory, config):
    challenge_config = config["challenge"]
    max_games = challenge_config.get("concurrency", 1)
    logger.info("You're now connected to {} and awaiting challenges.".format(config["url"]))
    manager = multiprocessing.Manager()
    challenge_queue = manager.list()
    control_queue = manager.Queue()
    control_stream = multiprocessing.Process(target=watch_control_stream, args=[control_queue, li])
    control_stream.start()
    busy_processes = 0
    queued_processes = 0

    with logging_pool.LoggingPool(max_games+1) as pool:
        while not terminated:
            event = control_queue.get()
            logger.info("||| control_queue event: %s" % event["type"])

            if event["type"] == "terminated":
                break

            elif event["type"] == "ping":
                li.pong()
            
            """
            elif event["type"] == "connected":
                for variant in challenge_config["variants"]:
                    logger.info("Creating seek for %s" % variant)
                    li.create_seek(variant)
            """

            elif event["type"] == "local_game_done":
                busy_processes -= 1
                logger.info("+++ Process Free. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))

            elif event["type"] == "challenge":
                chlng = model.Challenge(event["challenge"])
                if chlng.is_supported(challenge_config):
                    challenge_queue.append(chlng)
                    if (challenge_config.get("sort_by", "best") == "best"):
                        list_c = list(challenge_queue)
                        list_c.sort(key=lambda c: -c.score())
                        challenge_queue = list_c
                else:
                    try:
                        li.decline_challenge(chlng.id)
                        logger.info("    Decline {}".format(chlng))
                    except HTTPError as exception:
                        if exception.response.status_code != 404: # ignore missing challenge
                            raise exception

            elif event["type"] == "gameStart":
                if queued_processes <= 0:
                    logger.debug("Something went wrong. Game is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                game_id = event["game"]["id"]

                try:
                    skill_level = int(event["game"]["skill_level"])
                except Exception:
                    skill_level = 8

                try:
                    chess960 = event["game"]["chess960"] == "True"
                except Exception:
                    chess960 = False

                pool.apply_async(play_game, [li, game_id, control_queue, engine_factory, user_profile, config, challenge_queue, skill_level, chess960])
                busy_processes += 1
                logger.info("--- Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))

            elif event["type"] == "analysisStart":
                if queued_processes <= 0:
                    logger.debug("Something went wrong. Game analysis is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                game_id = event["game"]["id"]

                try:
                    skill_level = int(event["game"]["skill_level"])
                except Exception:
                    skill_level = 8

                try:
                    chess960 = event["game"]["chess960"] == "True"
                except Exception:
                    chess960 = False

                pool.apply_async(analyze_game, [li, game_id, control_queue, engine_factory, user_profile, config, skill_level, chess960, event["username"]])
                busy_processes += 1
                logger.info("--- Analysis Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))

            while ((queued_processes + busy_processes) < max_games and challenge_queue): # keep processing the queue until empty or max_games is reached
                chlng = challenge_queue.pop(0)
                try:
                    response = li.accept_challenge(chlng.id)
                    logger.info("    Accept {}".format(chlng))
                    queued_processes += 1
                    logger.info("--- Process Queue. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                except HTTPError as exception:
                    if exception.response.status_code == 404: # ignore missing challenge
                        logger.info("    Skip missing {}".format(chlng))
                    else:
                        raise exception
    logger.info("Terminated")
    control_stream.terminate()
    control_stream.join()

def analyze_game(li, game_id, control_queue, engine_factory, user_profile, config, skill_level, chess960, username):
    response = li.get_game_stream(game_id)
    lines = response.iter_lines()
    line0 = next(lines)
    print("line0 =", line0)
    while len(line0) == 0:
        line0 = next(lines)
    #Initial response of stream will be the full game info. Store it
    game = model.Game(json.loads(line0.decode('utf-8')), user_profile["username"], li.baseUrl, config.get("abort_time", 20))
    board = setup_board(game, chess960)
    engine = engine_factory(board)

    logger.info("+++ {}".format(game))

    engine.go_commands = {"movetime": 500, "depth": 13}

    while board.move_stack:
        best_move = engine.search(board, 0, 0, 0, 0)
        stats = engine.get_info()
        li.analysis(username, game_id, len(board.move_stack), "w" if board.color else "b", stats)
        board.pop()

@backoff.on_exception(backoff.expo, BaseException, max_time=600, giveup=is_final)
def play_game(li, game_id, control_queue, engine_factory, user_profile, config, challenge_queue, skill_level, chess960):
    response = li.get_game_stream(game_id)
    lines = response.iter_lines()
    line0 = next(lines)
    print("line0 =", line0)
    while len(line0) == 0:
        line0 = next(lines)
    #Initial response of stream will be the full game info. Store it
    game = model.Game(json.loads(line0.decode('utf-8')), user_profile["username"], li.baseUrl, config.get("abort_time", 20))
    board = setup_board(game, chess960)
    engine = engine_factory(board)
    conversation = Conversation(game, engine, li, __version__, challenge_queue)

    logger.info("+++ {}".format(game))

    engine_cfg = config["engine"]
    polyglot_cfg = engine_cfg.get("polyglot", {})
    book_cfg = polyglot_cfg.get("book", {})

    engine.set_skill_level(skill_level)

    try:
        if not polyglot_cfg.get("enabled") or not play_first_book_move(game, engine, board, li, book_cfg):
            play_first_move(game, engine, board, li)

        engine.set_time_control(game)

        for binary_chunk in lines:
            upd = json.loads(binary_chunk.decode('utf-8')) if binary_chunk else None
            u_type = upd["type"] if upd else "ping"
            if u_type == "chatLine":
                conversation.react(ChatLine(upd), game)
            elif u_type == "gameEnd":
                break
            elif u_type == "gameState":
                game.state = upd
                moves = upd["moves"].split()
                board = update_board(board, moves[-1])
                if not board.is_game_over() and is_engine_move(game, moves):
                    if config.get("fake_think_time") and len(moves) > 9:
                        delay = min(game.clock_initial, game.my_remaining_seconds()) * 0.015
                        accel = 1 - max(0, min(100, len(moves) - 20)) / 150
                        sleep = min(5, delay * accel)
                        time.sleep(sleep)
                    best_move = None
                    if polyglot_cfg.get("enabled") and len(moves) <= polyglot_cfg.get("max_depth", 8) * 2 - 1:
                        best_move = get_book_move(board, book_cfg)
                    if best_move == None:
                        best_move = engine.search(board, upd["wtime"], upd["btime"], upd["winc"], upd["binc"])
                    li.make_move(game.id, best_move)
                    game.abort_in(config.get("abort_time", 20))
            elif u_type == "ping":
                if game.should_abort_now():
                    logger.info("    Aborting {} by lack of activity".format(game.url()))
                    li.abort(game.id)
    except HTTPError as e:
        ongoing_games = li.get_ongoing_games()
        game_over = True
        for ongoing_game in ongoing_games:
            if ongoing_game["gameId"] == game.id:
                game_over = False
                break
        if not game_over:
            logger.warn("Abandoning game due to HTTP "+response.status_code)
    except (RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError) as exception:
        logger.error("Abandoning game due to connection error")
        traceback.print_exception(type(exception), exception, exception.__traceback__)
    finally:
        logger.info("--- {} Game over".format(game.url()))
        engine.quit()
        # This can raise queue.NoFull, but that should only happen if we're not processing
        # events fast enough and in this case I believe the exception should be raised
        control_queue.put_nowait({"type": "local_game_done"})


def play_first_move(game, engine, board, li):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        # need to hardcode first movetime since Lishogi has 30 sec limit.
        # best_move = engine.first_search(board, 10000)
        best_move = engine.first_search(board, 100)
        li.make_move(game.id, best_move)
        return True
    return False


def play_first_book_move(game, engine, board, li, config):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        book_move = get_book_move(board, config)
        if book_move:
            li.make_move(game.id, book_move)
            return True
        else:
            return play_first_move(game, engine, board, li)
    return False


def get_book_move(board, config):
    if board.uci_variant == "chess":
        book = config["standard"]
    else:
        if config.get("{}".format(board.uci_variant)):
            book = config["{}".format(board.uci_variant)]
        else:
            return None

    with chess.polyglot.open_reader(book) as reader:
        try:
            selection = config.get("selection", "weighted_random")
            if selection == "weighted_random":
                move = reader.weighted_choice(board).move()
            elif selection == "uniform_random":
                move = reader.choice(board, config.get("min_weight", 1)).move()
            elif selection == "best_move":
                move = reader.find(board, config.get("min_weight", 1)).move()
        except IndexError:
            # python-shogi raises "IndexError" if no entries found
            move = None

    if move is not None:
        logger.info("Got move {} from book {}".format(move, book))

    return move


def setup_board(game, chess960):
    VariantBoard = variants.VARIANT2BOARD[game.variant_name]
    board = VariantBoard(game.initial_fen, chess960=chess960)
    moves = game.state["moves"].split()
    for move in moves:
        board = update_board(board, move)
    return board


def is_white_to_move(game, moves):
    return len(moves) % 2 == (0 if game.white_starts else 1)


def is_engine_move(game, moves):
    return game.is_white == is_white_to_move(game, moves)


def update_board(board, move):
    board.push(move)
    return board

def intro():
    return r"""
    .   _/\_
    .  //o o\\
    .  ||   ||  lishogi-bot %s
    .  ||   ||
    .  ||___||  Play on Lishogi with a bot
    """ % __version__

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Play on Lishogi with a bot')
    parser.add_argument('-u', action='store_true', help='Add this flag to upgrade your account to a bot account.')
    parser.add_argument('-v', action='store_true', help='Verbose output. Changes log level from INFO to DEBUG.')
    parser.add_argument('--config', help='Specify a configuration file (defaults to ./config.yml)')
    parser.add_argument('-l', '--logfile', help="Log file to append logs to.", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, filename=args.logfile,
                        format="%(asctime)-15s: %(message)s")
    enable_color_logging(debug_lvl=logging.DEBUG if args.v else logging.INFO)
    logger.info(intro())
    CONFIG = load_config(args.config or "./config.yml")
    li = lishogi.Lishogi(CONFIG["token"], CONFIG["url"], __version__)

    user_profile = li.get_profile()
    username = user_profile["username"]
    is_bot = user_profile.get("title") == "BOT"
    logger.info("Welcome {}!".format(username))

    if args.u is True and is_bot is False:
        is_bot = upgrade_account(li)

    if is_bot:
        engine_factory = partial(engine_wrapper.create_engine, CONFIG)
        start(li, user_profile, engine_factory, CONFIG)
    else:
        logger.error("{} is not a bot account. Please upgrade it to a bot account!".format(user_profile["username"]))
