"""Configurações centrais do pipeline: paths, mapeamentos e regras de negócio."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths
# A pasta de saída é criada pelo main.py 
RAW = Path(os.environ.get("OLIST_RAW_PATH", Path("data") / "raw"))
PROCESSED = Path(os.environ.get("OLIST_PROCESSED_PATH", Path("data") / "processed"))

ARQUIVOS = {
    "orders": "olist_orders_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "translation": "product_category_name_translation.csv",
    "geo": "olist_geolocation_dataset.csv",
}

# Colunas de prefixo de CEP: lidas como TEXTO com 5 dígitos. Lidas como
# número, '01310' vira 1310 (24% dos clientes perdem o zero à esquerda).
COLUNAS_CEP = {
    "customers": "customer_zip_code_prefix",
    "sellers": "seller_zip_code_prefix",
    "geo": "geolocation_zip_code_prefix",
}

# Chave (grão) de cada tabela bruta -- usada na inspeção para contar
# duplicatas de CHAVE, que são as que podem duplicar pedidos num join.
CHAVES = {
    "orders": ["order_id"],
    "items": ["order_id", "order_item_id"],
    "customers": ["customer_id"],
    "sellers": ["seller_id"],
    "products": ["product_id"],
    "payments": ["order_id", "payment_sequential"],
    "reviews": ["review_id", "order_id"],
    "translation": ["product_category_name"],
}

# Integridade referencial a inspecionar nos dados brutos:
# (tabela_filha, coluna, tabela_pai, coluna_pai). As três últimas são
# "reversas" (pedidos sem item / sem pagamento / sem avaliação) e ajudam a
# entender o grão antes de qualquer join.
INTEGRIDADE = [
    ("items", "order_id", "orders", "order_id"),
    ("items", "product_id", "products", "product_id"),
    ("items", "seller_id", "sellers", "seller_id"),
    ("payments", "order_id", "orders", "order_id"),
    ("reviews", "order_id", "orders", "order_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("customers", "customer_zip_code_prefix", "geo", "geolocation_zip_code_prefix"),
    ("sellers", "seller_zip_code_prefix", "geo", "geolocation_zip_code_prefix"),
    ("products", "product_category_name", "translation", "product_category_name"),
    ("orders", "order_id", "items", "order_id"),
    ("orders", "order_id", "payments", "order_id"),
    ("orders", "order_id", "reviews", "order_id"),
]

# --- Regras de negócio (decisões explícitas e documentadas)

# Status de pedido. Decisão: o pipeline MANTÉM todos os pedidos -- cancelar
# também é um comportamento a analisar -- e SINALIZA os que não geram
# receita na coluna `pedido_valido_receita`. As medidas do Power BI filtram
# essa coluna. Um status fora de STATUS_CONHECIDOS interrompe o pipeline:
# é sinal de que a fonte mudou e a regra precisa ser revista.
STATUS_CONHECIDOS = {
    "created", "approved", "invoiced", "processing",
    "shipped", "delivered", "canceled", "unavailable",
}
STATUS_SEM_RECEITA = {"canceled", "unavailable"}

STATUS_PT = {
    "created": "Criado",
    "approved": "Aprovado",
    "invoiced": "Faturado",
    "processing": "Em processamento",
    "shipped": "Enviado",
    "delivered": "Entregue",
    "canceled": "Cancelado",
    "unavailable": "Indisponível",
}

# Recorrência (recompra), calculada só sobre pedidos válidos para receita:
#   'dia_diferente'   -> recompra = pedido válido em um DIA posterior ao da
#                        1ª compra. Vários pedidos no mesmo dia contam como
#                        uma única ocasião de compra (carrinho dividido não
#                        é retenção).
#   'qualquer_pedido' -> recompra = qualquer pedido válido depois do 1º.
RECOMPRA_REGRA = "dia_diferente"

# Estratégia para decidir a "categoria" de um pedido com itens de categorias
# diferentes (um pedido pode ter N itens de N categorias):
#   'valor'      -> categoria do item de maior preço (padrão)
#   'quantidade' -> categoria com maior quantidade de itens no pedido
# Empates são desfeitos pela ordem alfabética da categoria (determinístico).
CATEGORIA_PEDIDO_ESTRATEGIA = "valor"

# Bounding box aproximado do Brasil (latitude/longitude), usado para
# descartar coordenadas de geolocalização claramente erradas (fora do
# país) -- problema conhecido no dataset público da Olist.
BRASIL_LAT_MIN, BRASIL_LAT_MAX = -33.75, 5.27
BRASIL_LON_MIN, BRASIL_LON_MAX = -73.99, -34.79

# pandas `.dt.day_name()` sempre devolve em inglês, independente do locale do sistema
DIAS_SEMANA_PT = {
    "Monday":    (1, "Segunda-feira"),
    "Tuesday":   (2, "Terça-feira"),
    "Wednesday": (3, "Quarta-feira"),
    "Thursday":  (4, "Quinta-feira"),
    "Friday":    (5, "Sexta-feira"),
    "Saturday":  (6, "Sábado"),
    "Sunday":    (7, "Domingo"),
}

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# Tradução das formas de pagamento
FORMAS_PAGAMENTO_PT = {
    "credit_card": "Cartão de Crédito",
    "debit_card": "Cartão de Débito",
    "boleto": "Boleto",
    "voucher": "Voucher",
}

# Stopwords em português usadas na tokenização de reviews (nuvem de
# Ajustável: se termos genéricos demais (ex. "produto", "muito") estiverem
# dominando o "Top palavras" sem agregar sentido, adicione aqui.
# A comparação é feita sem acento ("está" e "esta" são a mesma stopword).
STOPWORDS_PT = frozenset("""
a as à às ao aos algum alguma algumas alguns também ainda até com como contra
da das de dela delas dele deles depois do dos e é em entre era eram essa
essas esse esses esta estas este estes estou está estamos estão estava
estavam eu foi fomos for fora foram isso isto já lhe lhes mais mas me mesmo
meu meus minha minhas muito na nas nem no nos nossa nossas nosso nossos num
numa o os ou para pela pelas pelo pelos por qual quando que quem se sem ser
seu seus só sua suas tem tém têm tenho ter teu teus tu tua tuas um uma você
vocês era foi tão vai vou vamos pra pro num numa outro outra outros outras
""".split())

# Colunas convertidas para dtype 'category': reduz memória e acelera
# groupby/merge em colunas de baixa cardinalidade (estado, tipo de
# pagamento, categoria de produto).
COLUNAS_CATEGORICAS = {
    "customers": ["customer_state"],
    "sellers": ["seller_state"],
    "payments": ["payment_type"],
    "products": ["product_category_name", "categoria_exibicao"],
}
