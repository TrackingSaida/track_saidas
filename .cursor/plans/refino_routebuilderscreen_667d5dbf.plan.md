---
name: Refino RouteBuilderScreen v2
overview: Refinamento avançado da tela RouteBuilderScreen com foco em UX operacional real (ordem da rota como elemento principal, rota parcial confirmada, polyline, marcadores profissionais, header inteligente e feedbacks).
todos: []
isProject: false
---

# Refino completo e aprimorado da RouteBuilderScreen

Este plano ajusta e melhora o planejamento anterior, elevando o nível da experiência para padrão profissional (nível Mercado Livre / logística real).

---

## PRINCÍPIO CENTRAL

**A ORDEM DA ROTA é o elemento principal.**

- No mapa: número da parada (ordem) em destaque nos marcadores.
- Na lista: ordem da rota como número principal em cada item.
- No detalhe (card do marcador): ordem visível quando relevante.

Regras:

- Sempre mostrar a **ordem da rota** como número principal (1, 2, 3…).
- Nunca usar "NA".
- Nunca usar índice interno de array como destaque.
- Nunca usar ID técnico (`id_saida`) como destaque visual.

---

## 1) Botão "Criar Rota" – com confirmação

**Onde:** EntregasListScreen (botão que leva ao RouteBuilder) e overlay do header na RouteBuilderScreen.

**Comportamento quando existem entregas sem endereço:**

Ao clicar em "Criar Rota":

1. Exibir **Alert** com duas opções:
  - Mensagem: *"X entregas não possuem endereço e não entrarão na rota."*
  - Botões: **[ Cancelar ]** **[ Criar Rota ]**
2. Somente se o usuário confirmar **[ Criar Rota ]**:
  - `setRouteDeliveries(deliveriesWithAddress)`
  - Navegar para RouteBuilder (no caso do EntregasListScreen) ou apenas atualizar a rota (no caso do header na RouteBuilderScreen).

**Se nenhuma entrega tiver endereço:**

- Exibir Alert: *"Nenhuma entrega possui endereço válido."*
- Não navegar e não alterar rota.

**Arquivos:** [EntregasListScreen.tsx](track_saida_mobile/src/features/entregas/screens/EntregasListScreen.tsx) (botão "Sugerir Rota" / "Criar Rota"), [RouteBuilderScreen.tsx](track_saida_mobile/src/screens/RouteBuilderScreen.tsx) (botão "Criar Rota" no overlay). Store: `deliveriesWithAddress`, `deliveriesWithoutAddress`, `setRouteDeliveries` em [deliveryStore.ts](track_saida_mobile/src/store/deliveryStore.ts).

---

## 2) Polyline no mapa

**Arquivo:** [DeliveryMap.tsx](track_saida_mobile/src/components/DeliveryMap.tsx).

- Importar `Polyline` de `react-native-maps`.
- Conectar coordenadas na **ordem do routeOrder** (usar `getOrderedRouteDeliveries` → filtrar com coords = `withCoords`).
- Renderizar apenas se `withCoords.length >= 2`.

**Configuração da linha:**

- `strokeWidth: 5`
- `strokeColor: colors.primary` (usar `useThemeColors()` no componente)
- `lineCap: "round"`
- `lineJoin: "round"`
- Opcional: `geodesic={true}` para curva geodésica.

A linha deve atualizar automaticamente ao:

- Reordenar via drag na lista (`reorderRoute`).
- Chamar "Otimizar" (`optimizeRoute`), pois ambos atualizam `routeOrder` no store.

---

## 3) Marcadores profissionais

**Arquivo:** [DeliveryMap.tsx](track_saida_mobile/src/components/DeliveryMap.tsx).

Remover qualquer uso de marcador "NA". Entregas sem coordenadas: **não renderizar marcador**.

**PENDENTE:**

- Fundo: cor do serviço (`ROUTE_MARKER_COLORS` em [routeUtils.ts](track_saida_mobile/src/features/entregas/utils/routeUtils.ts)).
- Número **grande** e central: **ordem da rota** (1, 2, 3…), não índice técnico.
- Borda branca.
- Sombra leve.

**ENTREGUE:**

- Fundo verde.
- Ícone ✓.

**AUSENTE:**

- Fundo vermelho.
- Ícone !.

**Primeira parada (ordem 1):** pode ter leve destaque visual (ex.: borda mais grossa ou anel discreto).

Cores de serviço e status já existem em `DeliveryMap` e `routeUtils`; garantir que o número exibido seja sempre a **posição na ordem da rota** (index + 1 na lista `ordered`).

---

## 4) Lista em formato etiqueta

**Arquivo:** [RouteBottomSheet.tsx](track_saida_mobile/src/components/RouteBottomSheet.tsx).

Cada item deve exibir:

- **Número da ORDEM da rota** em destaque (ex.: **[ 03 ]**).
- Badge do serviço (Shopee, Flex, Avulso).
- Nome do destinatário.
- Endereço resumido.
- Linha preparada: *"Pacotes nesta parada: X"* (ou *"—"* se o dado não existir no tipo/API).

**Estrutura ideal (exemplo):**

```
[ 03 ]  Shopee
Bruna
Av. Trindade, 122
Pacotes nesta parada: X   (ou "-")
```

Nunca usar ID interno como destaque. Ao arrastar:

- Atualizar `routeOrder` via `reorderRoute(data.map(d => d.id_saida))`.
- Mapa e Polyline atualizam sozinhos (mesmo store).

O tipo [EntregaListItem](track_saida_mobile/src/features/entregas/types.ts) não possui campo de pacotes hoje; deixar campo preparado (ex.: "Pacotes nesta parada: —" ou prop opcional) para quando o backend expor.

---

## 5) Header inteligente no mapa (overlay superior)

**Arquivo:** [RouteBuilderScreen.tsx](track_saida_mobile/src/screens/RouteBuilderScreen.tsx).

Substituir o header simples por um **overlay fixo** no topo contendo:

- **Totais:** total de paradas; distância estimada (km); tempo estimado (~ minutos), usando as funções do item 6.
- **Badge de estado da rota:**
  - 🟢 **Rota completa** — quando `deliveriesWithoutAddress.length === 0` (todas as pendentes com endereço estão na rota ou não há pendentes sem endereço no contexto).
  - 🟡 **Rota parcial** — quando `deliveriesWithoutAddress.length > 0` (existem entregas sem endereço que não entraram na rota).
  O contexto pode ser: na tela RouteBuilder, comparar se há `pendingDeliveries` sem endereço; se sim, mostrar "Rota parcial". Caso contrário, "Rota completa".
- **Botões:** **[ Otimizar ]** **[ Criar Rota ]**
- Manter **← Voltar** no overlay.

**Botão Otimizar:**

- Chamar `optimizeRoute()` do store.
- Exibir **Toast**: *"Rota otimizada com sucesso."*  
(React Native: usar `Alert` de curta duração ou lib de toast se o projeto já tiver; senão, `Alert.alert` com título positivo ou uma pequena mensagem temporária na UI.)

---

## 6) Cálculo de distância e tempo

**Arquivo:** [routeUtils.ts](track_saida_mobile/src/features/entregas/utils/routeUtils.ts).

Criar e exportar:

- `**computeRouteDistanceKm(orderedDeliveries)**`  
  - Entrada: array de `EntregaListItem` já na ordem da rota (apenas itens com `latitude` e `longitude`).  
  - Somar distâncias entre pontos consecutivos (fórmula de Haversine).  
  - Retornar distância total em km.
- `**computeRouteEstimatedMinutes(orderedDeliveries)**` (ou uma única função que retorne ambos)  
  - Regras: 2 minutos por parada + tempo de deslocamento a 30 km/h (distância em km / 30 * 60 = minutos).  
  - Retornar número de minutos (arredondado).

Retorno sugerido para uso no header:

```ts
{ distanceKm: number; estimatedMinutes: number }
```

Assinatura única pode ser:

```ts
export function computeRouteStats(orderedDeliveries: EntregaListItem[]): { distanceKm: number; estimatedMinutes: number }
```

Filtrar internamente apenas itens com `latitude` e `longitude` válidos.

---

## 7) Mapa sem dados

**Arquivo:** [DeliveryMap.tsx](track_saida_mobile/src/components/DeliveryMap.tsx) ou [RouteBuilderScreen.tsx](track_saida_mobile/src/screens/RouteBuilderScreen.tsx).

Se **nenhuma** entrega da rota possuir coordenadas (`withCoords.length === 0`):

- Não deixar o mapa vazio confuso.
- Exibir mensagem central (overlay sobre o mapa ou em vez do mapa):
  - *"Nenhuma entrega com endereço válido. Adicione endereços para montar sua rota."*

Condição: exibir quando há entregas na rota (`routeDeliveries.length > 0` ou `ordered.length > 0`) mas nenhuma com coordenadas.

---

## 8) Sincronização total

Mapa ↔ Lista devem estar sempre sincronizados via:

- `routeDeliveries`
- `routeOrder`
- `reorderRoute(order)`
- `optimizeRoute()`

Qualquer alteração de ordem ou conjunto de entregas:

- Atualiza a Polyline (mesma fonte `routeOrder` + `getOrderedRouteDeliveries`).
- Atualiza os marcadores (ordem e posição).
- Atualiza a lista (DraggableFlatList com `data={ordered}` e `onDragEnd` → `reorderRoute`).

Não duplicar estado: uma única fonte de verdade no store.

---

## 9) Regras importantes

- Não remover funcionalidades existentes (Voltar, bottom sheet, card do marcador, modais de ausente/navegar, marcar entregue, etc.).
- Não quebrar o fluxo atual de navegação (EntregasList → RouteBuilder, etc.).
- Melhorar apenas UX e clareza operacional.
- Manter arquitetura modular (DeliveryMap, RouteBottomSheet, RouteMarkerCard, routeUtils, store).
- Código organizado; componentes separados; sem lógica duplicada.

---

## Ordem sugerida de implementação

1. **routeUtils.ts:** funções de distância/tempo (Haversine + regras de tempo) e exportar `computeRouteStats` (ou equivalentes).
2. **DeliveryMap.tsx:** Polyline (config do item 2); refinamento dos marcadores (ordem em destaque, sem NA, primeira parada opcional); estado vazio com mensagem (item 7).
3. **RouteBottomSheet.tsx:** layout em formato etiqueta com ordem em destaque e linha "Pacotes nesta parada" (item 4).
4. **RouteBuilderScreen.tsx:** overlay do header com totais, badge Rota completa/parcial, botões Otimizar (com Toast) e Criar Rota (com Alert de confirmação) (itens 1 e 5).
5. **EntregasListScreen.tsx:** botão "Criar Rota" / "Sugerir Rota" com Alert de confirmação quando há entregas sem endereço (item 1).

---

## Objetivo final

Transformar a RouteBuilderScreen em:

- Tela profissional de construção de rota.
- Visual claro e logístico.
- Ordem da rota como elemento central em mapa, lista e contexto.
- Sincronização perfeita mapa/lista.
- Feedback visual adequado (confirmação, Toast, badge completa/parcial, estado vazio).
- Preparada para escalar (campo pacotes, possíveis extensões futuras).

