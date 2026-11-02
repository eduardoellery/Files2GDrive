# Estatísticas do site público

O site em `docs/` integra o Google Analytics 4 com o ID público
`G-K4FX8QK1YV`. A integração só executa no domínio
`files2gdrive.erservices.top`; a interface local do aplicativo não é alterada.

A tag só é carregada após aceite explícito. A recusa também é lembrada.
A preferência fica no `localStorage` (`files2gdrive-analytics-v1`); se o
armazenamento estiver indisponível, vale apenas para a página atual.
O botão “Preferências de cookies” no rodapé permite mudar a escolha.
A revogação desativa a medição e remove os cookies `_ga` e `_ga_*`
host-only com caminho `/`. A alteração é propagada às outras abas abertas.

Medimos as visualizações de página e o evento personalizado `download_click`
no link direto para o pacote. Esse evento não confirma transferência nem
instalação. A tag não envia eventos de clique anteriores ao consentimento.
As opções de medição otimizada e retenção de eventos são administradas na
propriedade GA4, fora do repositório. Sinais do Google e sinais de
personalização de anúncios são desativados pela tag. URLs de página enviadas
pela configuração excluem consulta e fragmento; links externos e outros
campos coletados pelo Google dependem também das opções da propriedade.

## Publicação e validação

A publicação segue o fluxo de branch, pull request e merge autorizado para
`main` do GitHub Pages. A existência do código não comprova recebimento de
eventos pela propriedade: a validação final precisa ocorrer após publicação.

1. Abra o site em janela privativa. Antes de aceitar, confira na aba Network
   que não há requisições a `googletagmanager.com` ou `google-analytics.com`.
2. Recuse, navegue entre início, privacidade e termos e confirme que a tag
   continua sem carregar.
3. Em “Preferências de cookies”, aceite. Confira o carregamento único de
   `gtag/js?id=G-K4FX8QK1YV` e a visualização no relatório Tempo real do GA4.
4. Clique no link direto do `.deb` e confira `download_click`. Bloqueadores
   de conteúdo podem impedir a coleta. Não confunda esse evento personalizado
   com o `file_download` da medição otimizada, se habilitado na propriedade.
5. Revogue, confira a remoção dos cookies e que novos cliques não geram eventos;
   recarregue a página e confirme que a tag não carrega. Repita com duas abas.
6. Confira a apresentação do painel em celular e sua navegação por teclado.

Referências: [configuração GA4](https://support.google.com/analytics/answer/9304153?hl=pt-BR),
[API da tag](https://developers.google.com/tag-platform/gtagjs/reference) e
[consentimento](https://developers.google.com/tag-platform/security/guides/consent).
