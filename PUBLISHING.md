# Preparar o código para um repositório público

O projeto usa a licença MIT. Preserve `LICENSE` nas cópias distribuídas; as
dependências mantêm suas respectivas licenças.

Para exportar somente os fontes e guias públicos revisados:

```bash
python3 scripts/export_public_source.py
```

O arquivo gerado em `dist/` contém README, licença, fontes, testes, scripts,
locks de dependências, guias e a captura de tela. O exportador usa uma lista
explícita de arquivos e tipos permitidos. Confira seu manifesto antes de publicar.

Extraia o arquivo em uma pasta separada para iniciar o repositório Git. Não use
o diretório de desenvolvimento inteiro como arquivo de distribuição. A exportação
não inclui histórico Git.

Ficam fora do código público: perfis `.runtime*`, notas em `.private-notes`,
ambientes virtuais, logs, bancos, tokens, configurações OAuth locais, instaladores
e relatórios privados. Esses dados não precisam ser apagados para publicar os
fontes. Uma revisão de privacidade deve ser repetida a cada nova publicação.

O instalador de teste é um artefato separado. Ele inclui a identificação OAuth
Desktop do aplicativo, que não é uma autorização de usuário; nunca inclui tokens
de acesso ou renovação. Distribuir esse instalador não libera novas contas no
projeto Google quando ele está em modo Testing. Quem mantiver uma distribuição
própria deve preparar seu próprio cliente OAuth conforme o guia do projeto.

Nenhum script deste projeto cria repositórios, faz push ou publica releases.
