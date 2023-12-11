# ChessAnalysisServer
A quick project to have a web server that calls out to a chess engine to analyze a position and returns the response in JSON.
Results are cached into a persistent sqlitedict file.

Sample request:
```
http://127.0.0.1:5000/analyze?depth=1&fen=r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R%20b%20KQkq%20-%203%203
```
Response:
```
{
  "cached": false,
  "depth": 1,
  "elapsed": 0.00980520248413086,
  "ev": 43,
  "fen": "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
  "move_san": "Nf6",
  "move_uci": "g8f6",
  "nodes": 60,
  "pv_san": "Nf6",
  "pv_uci": "g8f6",
  "white_wdl": 0.5315
}
```
