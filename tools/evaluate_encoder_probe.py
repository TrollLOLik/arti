"""Local pinned encoder probe on fixed synthetic text; provision weights separately."""
import asyncio,json,time,argparse
from pathlib import Path
from cognition.semantic import LocalEncoder,MODEL,REVISION
CASES=[
('work_conflict','Вчера поссорился с начальником из-за сроков сдачи проекта.','Вспомни тот неприятный разговор на работе'),
('lake_trip','В июле мы с Олей ездили отдыхать на озеро Тургояк.','Где мы отдыхали летом с Олей?'),
('lost_keys','Утром потерял ключи от квартиры и полчаса искал их у подъезда.','Когда я не мог попасть домой из-за пропажи?'),
('dog_walk','Каждый вечер гуляю с собакой Рексом в парке.','Что я обычно делаю с питомцем после работы?'),
('bike_repair','Андрей помог заменить колесо на велосипеде в воскресенье.','Кто помог мне починить велосипед на выходных?'),
('exam','Марина сдала экзамен по английскому и очень обрадовалась.','У кого получилось успешно пройти языковую проверку?'),
('cake','В пятницу испекли шоколадный торт на день рождения сестры.','Что готовили сладкого к семейному празднику?'),
]
async def main(model_dir, output):
 enc=LocalEncoder(model_dir)
 try:
  t=time.monotonic();v=await enc.encode([x[1] for x in CASES]+[x[2] for x in CASES],timeout=120);elapsed=time.monotonic()-t
  if v is None: raise RuntimeError('actual_encoder_unavailable')
  rows=[]
  for i,(key,source,q) in enumerate(CASES):
   scored=sorted([(sum(a*b for a,b in zip(v[len(CASES)+i],v[j])),item[0]) for j,item in enumerate(CASES)],reverse=True)
   rows.append(dict(case=key,expected=key,top=scored[0][1],passed=scored[0][1]==key,top_cosine=round(scored[0][0],5),expected_cosine=round(next(s for s,k in scored if k==key),5)))
  t=time.monotonic();warm=await enc.encode([CASES[0][2]],timeout=.6);warm_elapsed=time.monotonic()-t
  result=dict(model=MODEL,revision=REVISION,corpus='seven_fixed_synthetic_russian_paraphrases',examples_are_synthetic=True,network_during_inference=False,real_provider_calls=0,real_telegram_calls=0,cold_batch_seconds=elapsed,warm_query_seconds=warm_elapsed,warm_default_budget_success=warm is not None,passed=sum(x['passed'] for x in rows),total=len(rows),cases=rows,limitations=['Small synthetic top-1 embedding probe, not end-to-end SQL retrieval or independent human/held-out validation.'])
  result.update(metric='embedding_top1_sanity_not_retrieval_accuracy', semantic_cutoff=.42, above_semantic_cutoff=sum(x['expected_cosine']>=.42 for x in rows), human_pilot_completed=False)
  Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
 finally: await enc.close()
if __name__=='__main__':
 parser=argparse.ArgumentParser(description='Synthetic local embedding sanity probe; no model download or human-quality claim')
 parser.add_argument('--model-dir',required=True)
 parser.add_argument('--output',required=True)
 args=parser.parse_args()
 asyncio.run(main(args.model_dir,args.output))
