---
name: confirmacion
description: Léxico del clasificador de confirmación escrita del motor (HU-AOS-05 y HU-AOS-09, core/kernel/procedures/clasificador.py). Confirma una frase exacta de `si` o, sin signos de pregunta y con 8 palabras o menos, una frase hecha SOLO de `nucleo`, `prefijos` y `cortesia`, con al menos una de `nucleo` y ninguna de `negaciones`. Lo ambiguo ("sí", "dale", "ok", "listo" solos, "todo bien" sin una marca de confirmación) no confirma; "bien" solo acompaña a una de `nucleo` ("confirmo, todo bien"). También confirma una frase exacta de `si` con `cortesia` intercalada. Para confirmar un DATO (no una acción ni una etapa) basta un asentimiento: `prefijos`, `nucleo` o `asiente_dato`, con `objetos_de_dato` y `cortesia` alrededor, sin negaciones; esas dos secciones nunca cuentan para una acción. Se compara sin tildes, mayúsculas ni signos. Cambiar este léxico exige correr el set etiquetado (tests/fixtures/confirmacion/etiquetado.jsonl) con precisión 1.0.
---
## si
si confirmo
sí confirmo
confirmo
confirmado
sí confirmado
sí de acuerdo
sí está bien
sí correcto
correcto
sí es correcto
sí señor
sí señora
afirmativo
sí por favor
## nucleo
confirmo
confirmado
confirmada
confirmamos
acepto
aceptado
aceptada
autorizo
autorizado
correcto
correcta
es correcto
todo correcto
esta correcto
todo esta correcto
lo confirmo
afirmativo
asi es
procede
adelante
## prefijos
si
sii
sip
claro
perfecto
listo
ok
okay
vale
dale
bueno
de acuerdo
esta bien
## cortesia
bien
gracias
muchas gracias
mil gracias
por favor
porfa
porfavor
senor
senora
todo
el pedido
la compra
el pago
la orden
el cobro
mi pedido
mi compra
mi pago
los terminos
con el pago
con la compra
con el pedido
pasame el link
pasame el link de pago
mandame el link
mandame el link de pago
enviame el link
enviame el link de pago
espero el link
espero el link de pago
## negaciones
no
nop
nunca
jamas
tampoco
ni
pero
aunque
sino
excepto
menos
espera
esperame
espere
momento
todavia
aun
despues
luego
manana
mas tarde
cambia
cambiar
cambio
cancela
cancelar
cancelo
mejor
quizas
quiza
tal vez
creo
depende
duda
revisa
revisar
otro
otra
mal
incorrecto
error
## asiente_dato
bien
todo bien
asi esta bien
esta perfecto
estan bien
son correctos
es ese
ese es
## objetos_de_dato
el dato
los datos
mis datos
el correo
mi correo
el email
mi email
el nombre
mi nombre
el telefono
mi telefono
el celular
mi celular
el numero
mi numero
el documento
mi documento
la cedula
mi cedula
la direccion
mi direccion
el metodo de pago
el medio de pago
