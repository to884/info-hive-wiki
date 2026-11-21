# 利用者向け文書の CSS

`fluent2.css` は `fluent2-html-replica` 0.1.5 の同梱 CSS で、browse が使う版と同じです。元の Fluent UI の基準は `@fluentui/react-components` 9.75.2、`microsoft/fluentui` の `f9720c35ba893c9630f4c0389d07dc6d6fd6068d` です。著作権表示と許諾は CSS の先頭と [licenses](licenses/) にあります。

SHA-256 は `05d35361e0340e1029507beb90ce280a1db76a473e7aaf0d896436bf1f231fba` です。

browse の CSS が更新されたら、リポジトリのルートで同じ版を写します。

```powershell
Copy-Item -LiteralPath 'skills/info-hive/scripts/browse_assets/fluent2.css' -Destination 'docs/html/assets/fluent2.css'
Get-FileHash -Algorithm SHA256 -LiteralPath 'skills/info-hive/scripts/browse_assets/fluent2.css','docs/html/assets/fluent2.css'
```

両方のハッシュが一致することを確かめ、上のハッシュとライセンスの写しを更新します。文書の画面と印刷の指定は `manual.css` と `manual-print.css` にあります。

## コピーボタン

`manual-code.js` に Microsoft の `@fluentui/svg-icons` 1.1.345 の `copy_16_regular.svg` と `copy_16_filled.svg` のパスを収録しています。出どころと MIT ライセンスは [第三者の表示](licenses/fluent2-html-replica.THIRD-PARTY-NOTICES.txt) を参照してください。
