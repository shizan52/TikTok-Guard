"""Canned Portuguese/Spanish comments for `python run.py simulate`.

The real target channel used for the live demo (a Bangladeshi creator) gets
comments in Bengali/English, not Portuguese/Spanish -- so the PT/ES reply
quality the client actually cares about never showed up on screen during the
live scan. This feeds a few representative PT/ES comments (negative *and*
positive, so the demo also shows the bot correctly leaving good comments
alone) through the exact same classify -> draft -> Telegram-card pipeline.
"""

SIMULATED_COMMENTS = [
    {"cid": "sim-pt-1", "username": "cliente_pt_01",
     "text": "Esse produto é uma porcaria, nunca mais compro nada dessa marca, atendimento péssimo.",
     "digg_count": 12},
    {"cid": "sim-pt-2", "username": "cliente_pt_02",
     "text": "Péssima experiência, o produto chegou quebrado e ninguém me respondeu.",
     "digg_count": 5},
    {"cid": "sim-es-1", "username": "cliente_es_01",
     "text": "Este producto es una basura, pésimo servicio, no vuelvo a comprar nunca más.",
     "digg_count": 9},
    {"cid": "sim-es-2", "username": "cliente_es_02",
     "text": "Qué decepción, la entrega llegó tarde y nadie me ayudó a resolverlo.",
     "digg_count": 3},
    {"cid": "sim-pt-3", "username": "cliente_pt_03",
     "text": "Adorei o produto, chegou super rápido, parabéns equipe!",
     "digg_count": 20},
    {"cid": "sim-es-3", "username": "cliente_es_03",
     "text": "Me encantó el servicio, todo perfecto, gracias!",
     "digg_count": 15},
]

# Customer QUESTIONS: two the company information can answer (address, history), one in Spanish, and one it does NOT
# cover (opening hours) - the bot must say so politely and point to a direct message instead of inventing an answer.
SIMULATED_QUESTIONS = [
    {"cid": "sim-q-pt-1", "username": "cliente_pt_10",
     "text": "Qual o endereço da loja de Central City?", "digg_count": 4},
    {"cid": "sim-q-pt-2", "username": "cliente_pt_11",
     "text": "Vocês existem desde que ano? Parece que é uma loja bem antiga", "digg_count": 2},
    {"cid": "sim-q-es-1", "username": "cliente_es_10",
     "text": "¿Cuántas tiendas tienen en Examplia?", "digg_count": 1},
    {"cid": "sim-q-pt-3", "username": "cliente_pt_12",
     "text": "Que horas vocês abrem aos domingos?", "digg_count": 3},
]
SIMULATED_COMMENTS = SIMULATED_COMMENTS + SIMULATED_QUESTIONS
