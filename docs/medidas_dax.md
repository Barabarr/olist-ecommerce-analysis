# Medidas DAX

As principais medidas do relatório, agrupadas por tema. Os valores são os do período inteiro, sem filtros, e foram conferidos contra o mesmo cálculo feito em Python sobre os dados tratados.

## Padrões usados

**1. Filtro de pedidos válidos com `KEEPFILTERS`.** Quase toda medida considera só os pedidos válidos (sem cancelados e indisponíveis). O `KEEPFILTERS` faz esse filtro se somar aos filtros da página, em vez de substituí-los.

```DAX
Qtd Pedidos =
CALCULATE (
    DISTINCTCOUNT ( dataset_analitico[order_id] ),
    KEEPFILTERS ( dataset_analitico[pedido_valido_receita] = 1 )
)
```

**2. "Pedidos que contêm" com `CROSSFILTER`.** O filtro de produto ou vendedor não chega sozinho à tabela de pedidos. Essas medidas ligam a filtragem nos dois sentidos só dentro do cálculo. Por isso, um pedido com itens de duas categorias conta nas duas.

```DAX
% Reviews Negativas (pedidos que contêm) =
CALCULATE (
    [% Reviews Negativas],
    CROSSFILTER ( fato_itens[order_id], dataset_analitico[order_id], BOTH )
)
```

**3. Clientes contados pela pessoa, com `TREATAS`.** O `customer_id` muda a cada pedido. A medida pega os pedidos válidos no contexto e conta as pessoas (`customer_unique_id`) por trás deles.

```DAX
Qtd Clientes =
CALCULATE (
    DISTINCTCOUNT ( dim_cliente[customer_unique_id] ),
    TREATAS (
        CALCULATETABLE (
            VALUES ( dataset_analitico[customer_id] ),
            KEEPFILTERS ( dataset_analitico[pedido_valido_receita] = 1 )
        ),
        dim_cliente[customer_id]
    )
)
```

**4. `==` para comparar com zero.** Em DAX, vazio `= 0` é verdadeiro. Como `pedido_atrasado` fica vazio nos pedidos não entregues, "entregue no prazo" precisa de `== 0`, senão os não entregues entram na conta.

```DAX
% Reviews Negativas no Prazo (pedidos que contêm) =
CALCULATE (
    [% Reviews Negativas (pedidos que contêm)],
    dataset_analitico[pedido_atrasado] == 0
)
```

**5. Participação com coluna que tem ordenação personalizada.** Quando uma coluna usa "Classificar por coluna", o Power BI filtra também pela coluna de ordenação. Para calcular a participação de cada forma de pagamento, é preciso remover o filtro das duas.

```DAX
% Valor Pago por Forma =
DIVIDE (
    [Valor Pago (Pagamentos)],
    CALCULATE (
        [Valor Pago (Pagamentos)],
        REMOVEFILTERS (
            fato_pagamentos[forma_pagamento_exibicao],
            fato_pagamentos[forma_ordem]
        )
    )
)
```

## Principais medidas

| Tema | Medida | O que mede | Valor |
|---|---|---|---|
| Financeiro | Qtd Pedidos | Pedidos válidos | 98.207 |
| | Receita de Produtos | Soma do preço dos itens, sem frete | R$ 13.494.400,74 |
| | Valor Frete | Soma do frete | R$ 2.241.126,29 |
| | Ticket Médio | Valor pago por pedido (com frete) | R$ 160,26 |
| | % Frete sobre Produtos | Frete ÷ receita de produtos | 16,61% |
| | % Pedidos Parcelados | Pedidos com 2 ou mais parcelas | 51,51% |
| Logística | % Atrasados | Entregues depois da data prometida ÷ entregues | 6,77% |
| | Dias Médios de Atraso | Média de dias de atraso, só nos atrasados | 10,62 |
| | Prazo Médio de Entrega (dias) | Da compra até a entrega | 12,56 |
| | Antecedência Média vs Prazo Prometido (dias) | Quantos dias antes do prometido a entrega chega | 11,88 |
| | Distância Média (km) | Distância em linha reta entre cliente e vendedor | 597,5 |
| Avaliações | Nota Média | Média das notas de 1 a 5 | 4,07 |
| | % Reviews Negativas | Notas 1 ou 2 | 15,10% |
| | % Reviews Positivas | Notas 4 ou 5 | 76,62% |
| | % com Comentário | Avaliações com texto escrito | 41,78% |
| | % Reviews Negativas no Prazo (geral) | Negativas só entre entregues no prazo | 9,55% |
| Clientes | Qtd Clientes | Pessoas com pelo menos um pedido válido | 94.990 |
| | Clientes Recorrentes | Pessoas que compraram em mais de um dia | 2.065 |
| | % Clientes Recorrentes | Recorrentes ÷ clientes | 2,17% |
| Produtos | Qtd Itens | Itens de pedidos válidos | 112.101 |
| | Qtd Produtos | Produtos distintos vendidos | 32.729 |
| | Preço Médio por Item | Média do preço por item | R$ 120,38 |

## Medidas de apoio à visualização

- **`Texto Contexto Filtros`** monta a linha "Período: … · UF: …" exibida abaixo do título de cada página, para o leitor saber quais filtros estão ativos.
- **Medidas de cor** (`Cor Faixa Atraso`, `Cor Atraso UF`, `Cor Nota` e outras) devolvem o código da cor de cada barra ou ponto. Elas aplicam a regra visual do relatório: vermelho para atraso e insatisfação, azul para o dado neutro e cinza para o contexto.
