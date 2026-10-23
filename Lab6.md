余裕のある方は次を試してみましょう


web/src/Components/Notes/NoteDisplay.jsの16行目のconst accentsの内容を変更して、ビルドとデプロイ

``` 
npm run test+build
aws s3 sync --delete build/. s3://$webBucket
 ```
 
変更内容(japaneseを追加しています)

```
  const accents = {
    american: {display: 'American', voice: [{name: 'Joey'}, {name: 'Joanna'}]},
    australian: {display: 'Australian', voice: [{name: 'Russell'}, {name: 'Nicole'}]},
    british: {display: 'British', voice: [{name: 'Brian'}, {name: 'Amy'}]},
    indian: {display: 'Indian', voice: [{name: 'Raveena'}]},
    welsh:  {display: 'Welsh', voice: [{name: 'Geraint'}]},
    japanese:  {display: 'Japanese', voice: [{name: 'Takumi'}, {name: 'Mizuki'}]}
  }
```