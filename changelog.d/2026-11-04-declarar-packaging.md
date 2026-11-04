## Corregido

- Declara `packaging` como dependencia directa. kombu 5.7.0 lo importa en el transporte de redis sin declararlo; la imagen instala con `pip` sin lock y, al reconstruirse, dejaba al worker y al beat en bucle de reinicios (`ModuleNotFoundError: No module named 'packaging'`).
