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

O instalador público é um artefato separado. Ele inclui a identificação OAuth
Desktop do aplicativo, que não é uma autorização de usuário; nunca inclui tokens
de acesso ou renovação. O projeto OAuth da distribuição oficial está em produção,
com domínio verificado e site HTTPS, conforme atualização de 2 de novembro de 2026.
Quem mantiver uma distribuição própria deve preparar e publicar seu próprio cliente
OAuth conforme o guia; se esse projeto estiver em Testing, suas contas continuam
sujeitas à lista de testadores.

Nenhum script deste projeto cria repositórios, faz push ou publica releases.

## Fluxo de alterações

Toda atualização segue: branch de trabalho → commit → push da branch → pull request para `main` → revisão e verificações pertinentes → merge. Não enviar alterações diretamente para `main`.

Faça merge somente dentro do escopo autorizado pelo mantenedor e respeite proteções e verificações do repositório; não contorne bloqueios. Não é obrigatório gerar instalador em mudanças apenas de documentação. Releases e seus anexos continuam artefatos separados.

## Padrão de versões e releases

- Versão: `MAJOR.MINOR.PATCH`, conforme [SemVer](https://semver.org/lang/pt-BR/).
- Tag Git: `vMAJOR.MINOR.PATCH`; primeira release pública sem sufixo: `v0.1.0`.
- Pacote Debian e versão interna: `files2gdrive_0.1.0_amd64.deb` e `0.1.0`.
- Fontes públicos: `files2gdrive-source-0.1.0.tar.gz`; integridade: `SHA256SUMS`.
- Correções posteriores podem usar `0.1.1`; novas funcionalidades, `0.2.0`. A série `0.x` é de evolução inicial e não promete estabilidade da API antes de `1.0.0`.
- Publicar a versão normal como release do GitHub, sem o indicador de pré-release. Reservar sufixos como `-rc.1` para pré-lançamentos e representar versões Debian anteriores à final com `~rc1`.
- Gerar os artefatos a partir do commit mesclado e apontar a tag para esse mesmo commit. Não alterar anexos já publicados para mudar o conteúdo de uma versão existente; criar nova versão para qualquer correção.

O fluxo de instalação limpa, nova autorização e backup em outro Ubuntu foi validado pelo mantenedor antes da publicação de `0.1.0`. As validações automatizadas e de empacotamento complementam esse teste; políticas específicas do Workspace e outros ambientes continuam exigindo avaliação própria.
