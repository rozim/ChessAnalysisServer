from atexit import register
from concurrent.futures import ThreadPoolExecutor
import functools
import json
import os
import signal
import threading
from threading import Timer
import time
import sqlitedict

from absl import app as absl_app
from absl import flags

import chess
import chess.engine
from chess import WHITE, BLACK

from flask import Flask, jsonify, request, make_response, abort


flask_app = Flask(__name__)

COMMIT_FREQ = 60

HASH = 256
THREADS = 1  # reproducible

START = time.time()

global_lock = threading.Lock()
requests = 0
cache_commits = 0
cache_win = 0
cache_lose = 0
cache_dirty = False
cache = None
engine_pool = None

FLAGS = flags.FLAGS
flags.DEFINE_integer('workers', 4, '')
flags.DEFINE_string('cache_file', 'data/cache.db', '')
flags.DEFINE_string('engine', 'stockfish', '')

flags.DEFINE_string('host', '127.0.0.1',
                    'Host to listen on, or 0.0.0.0 for all interfaces')
flags.DEFINE_integer('port', 5000, '')


def shutdown_server():
    global engine_pool
    engine_pool.close()
    time.sleep(0.5)


def signal_handler(sig, frame):
    print('Received SIGTERM signal, shutting down...')
    shutdown_server()
    sys.exit(0)


class ChessEnginePool:
    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.pool = ThreadPoolExecutor(max_workers=max_workers)
        self.lock = threading.Lock()
        self.active_engines = set()
        self.update_last_active()

    def update_last_active(self):
        self.last_active = time.time()

    def get_engine(self):
        self.update_last_active()
        with self.lock:
            self.update_last_active()
            if not self.active_engines:
                engine = chess.engine.SimpleEngine.popen_uci(FLAGS.engine)
                # engine.ping()
                engine.configure({'Hash': HASH})
                engine.configure({'Threads': THREADS})
                engine.configure({'UCI_ShowWDL': 'true'})
                self.active_engines.add(engine)
            else:
                engine = self.active_engines.pop()
                # engine.ping()
            return engine

    def put_engine(self, engine):
        self.update_last_active()
        with self.lock:
            self.active_engines.add(engine)

    def close(self):
        # avoid 'RuntimeError: Set changed size during iteration'
        active_engines = self.active_engines.copy()
        for engine in active_engines:
            engine.quit()
            self.pool.shutdown(wait=True)
            self.active_engines.clear()

    def analyze_position(self, fen, depth):
        board = chess.Board(fen)
        future = self.pool.submit(self._analyze_position, board, depth)
        return future.result()

    def _analyze_position(self, board, depth):
        engine = self.get_engine()
        engine.configure({'Clear Hash': None})
        multi = engine.analyse(
            board, chess.engine.Limit(depth=depth), multipv=1)
        self.put_engine(engine)
        return multi


def simplify_pv(pv):
    return [move.uci() for move in pv]


def simplify_score(score, board):
    return score.pov(WHITE).score()


def simplify_multi2(multi, board):

    def _to_san(board, pv):
        board = board.copy()
        res = []
        for move in pv:
            res.append(board.san(move))
            board.push(move)
        return res

    for i, m in enumerate(multi):
        pv = m.get('pv', [])
        nodes = m.get('nodes', 0)
        if 'pv' not in m:
            continue
        assert 'score' in m, (m, 'multi=', multi, 'fen=', board.fen())

        res = {'ev': simplify_score(m['score'], board),
               'white_wdl': m['wdl'].pov(WHITE).expectation(),
               'best_move': pv[0].uci(),
               'best_san': board.san(pv[0]),
               'pv_san': _to_san(board, pv),
               'pv': simplify_pv(pv)}
        if i == 0:  # nodes
            res['nodes'] = nodes
        yield res


def simplify_fen(board):
    # rn2kbnr/ppq2pp1/4p3/2pp2Bp/2P4P/1Q6/P2NNPP1/3RK2R w Kkq - 2 13
    return ' '.join(board.fen().split(' ')[0:4])


@flask_app.route('/stats')
def stats():
    global requests, cache_commits, cache_win, cache_lose, global_lock
    with global_lock:
        return jsonify({'requests': requests,
                        'commits': cache_commits,
                        'cache_win': cache_win,
                        'cache_lose': cache_lose,
                        'cache_num_entries': len(cache),
                        'cache_bytes': os.stat(FLAGS.cache_file).st_size,
                        'uptime': int(time.time() - START)
                        })


@flask_app.route('/analyze')
def analyze_position():
    global requests, cache, engine_pool
    global cache_win, cache_lose, cache_dirty, cache_commits
    # Extract FEN and depth from URL arguments
    fen = request.args.get('fen')
    depth = request.args.get('depth')
    if not depth:
        abort(400, 'Need bad')
        return
    if not fen:
        abort(400, 'Need FEN')
        return

    depth = int(depth)
    if depth > 99:
        abort(400, 'Depth bad')
        return

    if len(fen) > 80 or len(fen) < 10 or len(fen.split(' ')) > 6:
        abort(400, 'Bad FEN')

    board = chess.Board(fen)
    key = f'{simplify_fen(board)}|{depth}'

    t1 = time.time()
    with global_lock:
        requests += 1
        multi = cache.get(key, None)
        if multi:
            cache_win += 1
        else:
            cache_lose += 1
    if multi:
        cached = True
    else:
        cached = False
        multi = engine_pool.analyze_position(fen, depth)
        multi = list(simplify_multi2(multi, board))
        with global_lock:
            cache[key] = multi
            cache_dirty = True
            if requests % COMMIT_FREQ == 0:
                cache.commit()
                cache_commits += 1
                cache_dirty = False
    dt = time.time() - t1

    json_response = jsonify({
        'cached': cached,
        'fen': fen,
        'depth': depth,
        'elapsed': dt,
        'ev': multi[0]['ev'],
        'white_wdl': multi[0]['white_wdl'],
        'pv_uci': ' '.join(multi[0]['pv']),
        'pv_san': ' '.join(multi[0]['pv_san']),
        'move_uci': multi[0]['pv'][0],
        'move_san': multi[0]['pv_san'][0],
        'nodes': multi[0]['nodes'],
    })
    response = make_response(json_response)
    response.headers['Cache-Control'] = 'public, max-age=31536000'

    return response


def tick():
    global cache, global_lock, cache_commits, cache_dirty
    with global_lock:
        if cache_dirty:
            print('COMMIT')
            cache.commit()
            cache_dirty = False
            cache_commits += 1
    Timer(60, tick).start()


# def shutdown_inactive_engines(engine_pool):
#   Timer(10, functools.partial(shutdown_inactive_engines, engine_pool))


def main(argv):
    global engine_pool, cache

    assert os.path.exists(FLAGS.engine), FLAGS.engine

    # import logging
    # logging.basicConfig(level=logging.DEBUG)

    # Make sure engine exists.
    engine = chess.engine.SimpleEngine.popen_uci(FLAGS.engine)
    engine.ping()
    engine.quit()

    engine_pool = ChessEnginePool(max_workers=FLAGS.workers)

    cache = sqlitedict.open(filename=FLAGS.cache_file,
                            flag='c',
                            encode=json.dumps,
                            decode=json.loads)

    Timer(60, tick).start()
    # Timer(10, functools.partial(shutdown_inactive_engines, engine_pool))

    register(shutdown_server)
    signal.signal(signal.SIGTERM, signal_handler)

    flask_app.run(debug=False, host=FLAGS.host, port=FLAGS.port)


if __name__ == '__main__':
    absl_app.run(main)
